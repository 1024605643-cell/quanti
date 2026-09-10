"""Portable Windows paper terminal. GUI never waits for network requests."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import threading
import time
import uuid
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
import tkinter as tk
from tkinter import ttk, messagebox

from quanti.desktop_settings import APP_DIR, load_settings, save_settings

BEIJING = timezone(timedelta(hours=8))
REPORT_URL = 'https://1024605643-cell.github.io/quanti/quant/latest.json'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


class Engine:
    def __init__(self, directory=APP_DIR, fetch=None, start_worker=True):
        from scripts import paper_trade
        self.paper = paper_trade
        self.directory = directory
        self.path = directory / 'account.json'
        self.lock = threading.RLock()
        self.events = queue.Queue()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.running = False
        self.scanning = False
        self.fetch = fetch or paper_trade.fetch_snapshot
        self.pool = ThreadPoolExecutor(max_workers=3)
        self.state = (json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists()
                      else {'cash': 100000, 'positions': {}, 'trades': [], 'orders': []})
        self.settings = load_settings(directory)
        self.thread = threading.Thread(target=self.loop, daemon=True)
        if start_worker:
            self.thread.start()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def submit(self, side, code, fraction):
        started = time.perf_counter()
        with self.lock:
            state = self.snapshot()
            before = len(state.get('orders', []))
            self.paper.submit(state, side, code, fraction, 'desktop-' + uuid.uuid4().hex,
                              datetime.now(BEIJING))
            write_json(self.path, state)
            self.state = state
            duplicate = len(state.get('orders', [])) == before
        self.wake.set()
        elapsed = (time.perf_counter() - started) * 1000
        self.events.put(('status', f"{'已有相同委托，未重复提交' if duplicate else '委托已保存到本机'} · {elapsed:.0f} ms；等待监控取得新行情"))
        return elapsed

    def tick(self):
        state = self.snapshot()
        codes = set(state['positions']) | {o['code'] for o in state.get('orders', []) if o['status'] == 'pending'}
        if not codes:
            return
        start = time.perf_counter()
        futures = {code: self.pool.submit(self.fetch, code) for code in codes}
        quotes = {}
        for code, future in futures.items():
            try:
                quotes[code] = future.result(timeout=12)
            except Exception:
                pass
        with self.lock:
            if not self.running:
                return
            state = self.snapshot()
            before = {o['id']: o['status'] for o in state.get('orders', [])}
            old_alerts = state.get('alerts', [])
            old_history = len(state.get('equity_history', []))
            self.paper.refresh(state, fetch=lambda code: quotes[code])
            # Store at most one equity sample per minute, even with 2s quote polling.
            history = state.get('equity_history', [])
            if old_history and len(history) > old_history and history[-1]['time'][:16] == history[-2]['time'][:16]:
                history[-2:] = history[-1:]
            write_json(self.path, state)
            self.state = state
        elapsed = (time.perf_counter() - start) * 1000
        self.events.put(('status', f'行情检查 {elapsed:.0f} ms · {len(quotes)}/{len(codes)}只取得报价 · {datetime.now(BEIJING):%H:%M:%S}'))
        receipts = [f"{o['code']}：{o['reason']}" for o in state.get('orders', [])
                    if o['status'] != before.get(o['id'])]
        if state.get('alerts') != old_alerts:
            receipts += [f"{a['code']}：{a['why']}；请在软件确认卖出{a['fraction']:.0%}当前仓位"
                         for a in state.get('alerts', [])]
        if receipts:
            text = '\n'.join(receipts)
            self.events.put(('receipt', text))
            threading.Thread(target=self.notify, args=(text,), daemon=True).start()

    def notify(self, text):
        url = self.settings.get('WECOM_WEBHOOK_URL', '')
        if not url:
            return
        try:
            body = json.dumps({'msgtype': 'text', 'text': {'content': '本机模拟盘\n' + text}}).encode()
            with urlopen(Request(url, data=body, headers={'Content-Type': 'application/json'}), timeout=10) as r:
                result = json.load(r)
            if result.get('errcode') != 0:
                raise ValueError('Rejected')
        except Exception:
            self.events.put(('receipt', '企业微信发送失败，委托状态已保存在本机，请查看软件账本。'))

    def loop(self):
        while not self.closed.is_set():
            if self.running:
                try:
                    self.tick()
                except Exception as exc:
                    self.events.put(('status', '本轮检查失败，账本保留：' + type(exc).__name__))
            self.wake.wait(2 if self.running else 30)
            self.wake.clear()

    def report(self, local=False):
        if self.scanning:
            return
        self.scanning = True
        def task():
            try:
                if local:
                    self.events.put(('status', '本机正在选股，可能需几分钟；模拟交易仍独立运行。'))
                    for key, value in self.settings.items():
                        os.environ[key] = value
                    from scripts import short_term_daily as research
                    candidates, rejected = research.scan()
                    report = dict(generated_at=datetime.now(BEIJING).isoformat(), candidates=candidates,
                                  rejected=rejected, ai_review=research.ai_review(candidates),
                                  wencai_status=research.WENCAI_STATUS, alerts=self.snapshot().get('alerts', []))
                else:
                    with urlopen(REPORT_URL, timeout=12) as r:
                        report = json.load(r)
                write_json(self.directory / 'report.json', report)
                self.events.put(('report', report))
                if local:
                    self.notify('本机选股已完成，请在软件查看候选和风险说明。')
                    try:
                        from scripts.short_term_daily import render, notify_email
                        notify_email('本机A股短线选股报告', render(report))
                    except Exception:
                        self.events.put(('status', '本机报告已保存，QQ邮件发送失败，请检查邮箱设置。'))
            except Exception as exc:
                self.events.put(('status', '选股报告暂未更新：' + type(exc).__name__ + '；模拟委托不受影响。'))
            finally:
                self.scanning = False
        threading.Thread(target=task, daemon=True).start()

    def close(self):
        with self.lock:
            self.running = False
            self.closed.set()
        self.wake.set()
        self.pool.shutdown(wait=False, cancel_futures=True)


class App:
    def __init__(self, root, directory=APP_DIR):
        self.root, self.directory = root, directory
        self.engine = None
        self.init_events = queue.Queue()
        self.account_signature = None
        self.last_report_sync = time.monotonic()
        self.nav_buttons = []
        root.title('Quanti 短线模拟盘 · 本机版 v0.1.1')
        root.geometry('1280x860')
        root.minsize(1060, 740)
        root.configure(bg='#f2f5fa')
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background='#f2f5fa', foreground='#243247')
        style.configure('Card.TFrame', background='white')
        style.configure('Card.TLabel', background='white')
        style.configure('Muted.TLabel', background='white', foreground='#6d7c91')
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 20, 'bold'))
        style.configure('TButton', padding=(13, 8), borderwidth=0, background='#e7edf6')
        style.map('TButton', background=[('active', '#d8e4f5')])
        style.configure('Primary.TButton', background='#2563eb', foreground='white')
        style.map('Primary.TButton', background=[('disabled', '#cbd5e1'), ('active', '#1d4ed8')],
                  foreground=[('disabled', '#64748b'), ('!disabled', 'white')])
        style.configure('TEntry', padding=8, fieldbackground='white', bordercolor='#d8e0ec')
        style.configure('TCombobox', padding=7, fieldbackground='white', bordercolor='#d8e0ec')
        style.configure('Treeview', rowheight=38, fieldbackground='white', background='white',
                        borderwidth=0, font=('Microsoft YaHei UI', 10))
        style.configure('Treeview.Heading', font=('Microsoft YaHei UI', 10, 'bold'),
                        padding=(10, 12), background='#edf2f9', relief='flat')
        style.map('Treeview', background=[('selected', '#dbeafe')], foreground=[('selected', '#163c7c')])
        style.layout('Workspace.TNotebook.Tab', [])
        style.configure('Workspace.TNotebook', background='white', borderwidth=0)

        sidebar = tk.Frame(root, bg='#142338', width=185)
        sidebar.pack(side='left', fill='y')
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text='Q / QUANTI', bg='#142338', fg='white',
                 font=('Segoe UI', 19, 'bold')).pack(anchor='w', padx=22, pady=(32, 4))
        tk.Label(sidebar, text='A股 · 短线研究与模拟', bg='#142338', fg='#9aaec9',
                 font=('Microsoft YaHei UI', 9)).pack(anchor='w', padx=22, pady=(0, 32))
        self.nav_container = tk.Frame(sidebar, bg='#142338')
        self.nav_container.pack(fill='x', padx=12)
        self.config_summary = tk.StringVar(value='正在读取接口配置')
        tk.Label(sidebar, text='v0.1.1 · 模拟 · 人工确认', bg='#142338', fg='#a5b5cc',
                 font=('Microsoft YaHei UI', 9)).pack(side='bottom', anchor='w', padx=22, pady=(6, 22))
        tk.Label(sidebar, textvariable=self.config_summary, bg='#142338', fg='#78ddc6',
                 font=('Microsoft YaHei UI', 9)).pack(side='bottom', anchor='w', padx=22)

        outer = ttk.Frame(root, padding=(26, 22, 26, 14))
        outer.pack(fill='both', expand=True)
        header = ttk.Frame(outer)
        header.pack(fill='x')
        ttk.Label(header, text='短线模拟盘', style='Title.TLabel').pack(side='left')
        self.monitor_badge = tk.Label(header, text='● 监控已暂停', bg='#fff0d9', fg='#946215',
                                     font=('Microsoft YaHei UI', 10), padx=13, pady=7)
        self.monitor_badge.pack(side='right')
        ttk.Label(outer, text='量化排名供你研究，买卖由你确认。委托与账户保存在这台电脑。',
                  foreground='#6d7c91').pack(anchor='w', pady=(6, 18))

        metrics = ttk.Frame(outer)
        self.metrics_panel = metrics
        metrics.pack(fill='x', pady=(0, 16))
        self.metric_values = {}
        for i, (key, label) in enumerate([('cash', '可用现金 / 元'), ('equity', '总资产 / 元'),
                                         ('return', '累计收益 / %'), ('drawdown', '采样最大回撤 / %')]):
            metrics.columnconfigure(i, weight=1, uniform='metric')
            card = ttk.Frame(metrics, style='Card.TFrame', padding=(16, 13))
            card.grid(row=0, column=i, sticky='nsew', padx=(0 if i == 0 else 10, 0))
            ttk.Label(card, text=label, style='Muted.TLabel').pack(anchor='w')
            var = self.metric_values[key] = tk.StringVar(value='—')
            ttk.Label(card, textvariable=var, style='Card.TLabel',
                      font=('Segoe UI', 21, 'bold')).pack(anchor='w', pady=(5, 0))

        toolbar = ttk.Frame(outer)
        self.toolbar = toolbar
        toolbar.pack(fill='x', pady=(0, 12))
        self.run_button = ttk.Button(toolbar, text='启动本机监控', command=self.toggle,
                                     style='Primary.TButton', state='disabled')
        self.run_button.pack(side='left', padx=(0, 8))
        ttk.Button(toolbar, text='更新云端候选', command=lambda: self.engine and self.engine.report()).pack(side='left', padx=4)
        ttk.Button(toolbar, text='本机重新选股', command=lambda: self.engine and self.engine.report(True)).pack(side='left', padx=4)
        self.report_timestamp = tk.StringVar(value='尚未载入报告')
        ttk.Label(toolbar, textvariable=self.report_timestamp, foreground='#6d7c91').pack(side='right')

        trade = ttk.Frame(outer, style='Card.TFrame', padding=(16, 12))
        self.trade_panel = trade
        trade.pack(fill='x', pady=(0, 14))
        self.side, self.code, self.fraction = tk.StringVar(value='买入'), tk.StringVar(), tk.StringVar(value='50%')
        ttk.Label(trade, text='模拟委托', style='Card.TLabel', font=('Microsoft YaHei UI', 11, 'bold')).grid(row=0, column=0, sticky='w', padx=(0, 14))
        ttk.Combobox(trade, textvariable=self.side, values=['买入', '卖出', '撤销待成交委托'],
                     state='readonly', width=16).grid(row=0, column=1, padx=4)
        ttk.Label(trade, text='股票代码', style='Card.TLabel').grid(row=0, column=2, padx=(10, 4))
        ttk.Entry(trade, textvariable=self.code, width=11).grid(row=0, column=3, padx=4)
        ttk.Combobox(trade, textvariable=self.fraction, values=['25%', '50%', '100%'],
                     state='readonly', width=7).grid(row=0, column=4, padx=4)
        self.submit_button = ttk.Button(trade, text='确认提交', command=self.submit,
                                        style='Primary.TButton', state='disabled')
        self.submit_button.grid(row=0, column=5, padx=8)
        ttk.Label(trade, text='选择列表中的股票可填入代码。买入按可用现金比例，卖出按该股股数比例；取得有效新报价后才模拟成交。',
                  style='Muted.TLabel', wraplength=850).grid(row=1, column=0, columnspan=6, sticky='w', pady=(9, 0))

        self.status = tk.StringVar(value='正在载入账本与加密配置…')
        ttk.Label(outer, textvariable=self.status, wraplength=950, foreground='#52677f').pack(side='bottom', anchor='w', pady=(10, 0))
        self.tabs = ttk.Notebook(outer, style='Workspace.TNotebook')
        self.tabs.pack(fill='both', expand=True)
        self.candidate_tree = self.table('选股候选', ['代码', '名称', '分数', '报告价格', '涨跌幅', '量价理由'])
        self.candidate_tree.bind('<<TreeviewSelect>>', self.select_code)
        self.position_tree = self.table('当前持仓', ['代码', '名称', '股数', '含费成本', '最近价格'])
        self.position_tree.bind('<<TreeviewSelect>>', self.select_code)
        self.order_tree = self.table('委托与成交', ['代码', '操作', '状态', '提交时间', '说明'])
        self.order_tree.bind('<<TreeviewSelect>>', self.select_code)
        report_frame = self.page('复盘与风险', '报告、未入选原因与需要复核的风险。候选价格来自报告，不是当前成交价。')
        self.report_text = self.text_area(report_frame)
        self.report_text.insert('end', '等待载入报告。新闻公告风险与历史回测尚未完整核验。')

        settings_page = self.page('接口设置', '星号表示密钥已隐藏。已保存仅表示本机存在配置，不代表接口已连通。')
        canvas = tk.Canvas(settings_page, bg='white', highlightthickness=0)
        scroll = ttk.Scrollbar(settings_page, orient='vertical', command=canvas.yview)
        scroll.pack(side='right', fill='y')
        canvas.pack(fill='both', expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        self.settings_frame = inner = ttk.Frame(canvas, style='Card.TFrame', padding=(4, 4, 16, 12))
        window = canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
        inner.columnconfigure(1, weight=1)
        self.fields, self.field_status = {}, {}
        fields = [('EMAIL_SENDER', 'QQ邮箱', False), ('EMAIL_PASSWORD', 'SMTP授权码', True),
                  ('LLM_PRIMARY_BASE_URL', '模型服务地址', False), ('LLM_PRIMARY_MODEL', '模型名称', False),
                  ('LLM_PRIMARY_API_KEY', '模型API密钥', True), ('WECOM_WEBHOOK_URL', '企业微信Webhook', True),
                  ('WENCAI_COOKIE', '问财Cookie', True)]
        for i, (key, label, secret) in enumerate(fields):
            ttk.Label(inner, text=label, style='Card.TLabel').grid(row=i, column=0, sticky='w', padx=(0, 12), pady=10)
            var = self.fields[key] = tk.StringVar()
            ttk.Entry(inner, textvariable=var, show='●' if secret else '', width=30).grid(row=i, column=1, sticky='ew', pady=6)
            status = self.field_status[key] = tk.StringVar(value='载入中')
            ttk.Label(inner, textvariable=status, style='Muted.TLabel').grid(row=i, column=2, padx=(12, 0))
        buttons = ttk.Frame(inner, style='Card.TFrame')
        buttons.grid(row=7, column=0, columnspan=3, sticky='w', pady=(15, 8))
        self.save_button = ttk.Button(buttons, text='加密保存设置', style='Primary.TButton', command=self.save_settings, state='disabled')
        self.save_button.pack(side='left', padx=(0, 10))
        ttk.Button(buttons, text='重新载入已保存配置', command=self.reload_settings).pack(side='left')
        ttk.Label(inner, text='配置绑定当前Windows用户；不随ZIP公开。问财若显示HTTP 403，请查看报告中的接口状态，该次查询不会参与加分。',
                  style='Muted.TLabel', wraplength=720).grid(row=8, column=0, columnspan=3, sticky='w', pady=(8, 0))
        log_frame = self.page('运行日志', '本次运行中的状态、成交回执与提醒。历史成交以“委托与成交”中的账本记录为准。')
        self.log_text = self.text_area(log_frame)
        self.log_text.insert('end', '软件关闭或电脑休眠后停止检查。−5%为清仓提醒阈值，所有买卖仍需人工确认；未连接券商实盘。\n')
        self.select_page(0)
        root.protocol('WM_DELETE_WINDOW', self.close)
        threading.Thread(target=self.initialize, daemon=True).start()
        root.after(150, self.poll)

    def page(self, title, description):
        frame = ttk.Frame(self.tabs, style='Card.TFrame', padding=(18, 15))
        self.tabs.add(frame, text=title)
        index = len(self.nav_buttons)
        button = tk.Button(self.nav_container, text=title, anchor='w', relief='flat', borderwidth=0,
                           bg='#142338', fg='#b9c8dc', activebackground='#263d5b', activeforeground='white',
                           font=('Microsoft YaHei UI', 11), padx=16, pady=12, cursor='hand2',
                           command=lambda: self.select_page(index))
        button.pack(fill='x', pady=3)
        self.nav_buttons.append(button)
        ttk.Label(frame, text=title, style='Card.TLabel', font=('Microsoft YaHei UI', 14, 'bold')).pack(anchor='w')
        ttk.Label(frame, text=description, style='Muted.TLabel', wraplength=880).pack(anchor='w', pady=(5, 13))
        return frame

    def select_page(self, index):
        self.tabs.select(index)
        if index == 4:
            self.metrics_panel.pack_forget()
            self.trade_panel.pack_forget()
        elif not self.metrics_panel.winfo_manager():
            self.metrics_panel.pack(fill='x', pady=(0, 16), before=self.toolbar)
            self.trade_panel.pack(fill='x', pady=(0, 14), before=self.tabs)
        for i, button in enumerate(self.nav_buttons):
            button.configure(bg='#294261' if i == index else '#142338', fg='white' if i == index else '#b9c8dc')

    def text_area(self, frame):
        text = tk.Text(frame, wrap='word', font=('Microsoft YaHei UI', 11), background='white',
                       foreground='#243247', relief='flat', padx=4, pady=6, spacing3=8)
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        return text

    def table(self, title, columns):
        descriptions = {'选股候选': '前5项为量化主推，仍需复核风险。选中股票后可在上方提交模拟委托。',
                        '当前持仓': '本机账户中的模拟持仓。行情缺失时显示待估值，不把缺失报价当成零。',
                        '委托与成交': '黄色为等待，绿色为成交。待成交不等于失败；相同委托无需重复提交。'}
        frame = self.page(title, descriptions[title])
        tree = ttk.Treeview(frame, columns=columns, show='headings', selectmode='browse')
        for i, col in enumerate(columns):
            tree.heading(col, text=col)
            tree.column(col, width=105 if i < len(columns)-1 else 320, minwidth=65,
                        stretch=i == len(columns)-1, anchor='w' if i in (0, 1, len(columns)-1) else 'center')
        tree.tag_configure('stripe', background='#f6f9fd')
        tree.tag_configure('pending', foreground='#976517')
        tree.tag_configure('filled', foreground='#087c64')
        tree.tag_configure('rejected', foreground='#b7424b')
        tree.tag_configure('expired', foreground='#8994a5')
        tree.tag_configure('cancelled', foreground='#8994a5')
        scroll = ttk.Scrollbar(frame, command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        horizontal.pack(side='bottom', fill='x')
        scroll.pack(side='right', fill='y')
        tree.pack(side='left', fill='both', expand=True)
        return tree

    def apply_settings(self, settings):
        for key, var in self.fields.items():
            var.set(settings.get(key, ''))
            self.field_status[key].set('已保存' if settings.get(key) else '未配置')
        self.config_summary.set(f'接口配置 {sum(bool(settings.get(k)) for k in self.fields)}/{len(self.fields)} 已保存')

    def reload_settings(self):
        if not self.engine:
            return
        try:
            self.engine.settings = load_settings(self.directory)
            self.apply_settings(self.engine.settings)
            self.status.set('已载入本机加密配置，密钥仍以圆点隐藏。')
        except Exception as exc:
            messagebox.showerror('配置读取失败', type(exc).__name__, parent=self.root)

    def initialize(self):
        try:
            self.init_events.put(('engine', Engine(self.directory)))
        except Exception as exc:
            self.init_events.put(('error', type(exc).__name__))

    def select_code(self, event):
        selected = event.widget.selection()
        if selected:
            self.code.set(str(event.widget.item(selected[0], 'values')[0]).zfill(6))

    def toggle(self):
        with self.engine.lock:
            self.engine.running = not self.engine.running
        self.run_button.config(text='暂停本机监控' if self.engine.running else '启动本机监控')
        self.monitor_badge.configure(text='● 本机监控运行中' if self.engine.running else '● 监控已暂停',
                                     bg='#d9f4ec' if self.engine.running else '#fff0d9',
                                     fg='#087c64' if self.engine.running else '#946215')
        self.status.set('本机监控已启动，约每2秒检查新行情。' if self.engine.running else '监控已暂停；现有委托保留。')
        self.engine.wake.set()

    def submit(self):
        side = {'买入': 'buy', '卖出': 'sell', '撤销待成交委托': 'cancel'}[self.side.get()]
        code = self.code.get().strip()
        fraction = float(self.fraction.get().rstrip('%')) / 100
        if not messagebox.askokcancel('确认模拟委托', f'{self.side.get()} {code}\n比例：{self.fraction.get()}\n仅模拟交易，程序取得有效新报价后才成交。', parent=self.root):
            return
        try:
            self.engine.submit(side, code, fraction)
            if not self.engine.running:
                self.status.set('委托已保存。当前监控暂停，请点击“启动本机监控”处理。')
        except ValueError as exc:
            messagebox.showinfo('委托未新增', str(exc), parent=self.root)
        except Exception as exc:
            messagebox.showerror('保存失败', type(exc).__name__ + '：请检查数据目录权限，勿重复提交。', parent=self.root)

    def save_settings(self):
        if not self.engine:
            return
        try:
            settings = {key: var.get().strip() for key, var in self.fields.items()}
            save_settings(settings, self.directory)
            if self.engine:
                self.engine.settings = settings
            self.apply_settings(settings)
            self.status.set('设置已加密保存。')
        except Exception as exc:
            messagebox.showerror('保存失败', type(exc).__name__, parent=self.root)

    def show_report(self, report):
        self.report_timestamp.set('报告 ' + report.get('generated_at', '时间未知')[:16].replace('T', ' '))
        self.candidate_tree.delete(*self.candidate_tree.get_children())
        for i, c in enumerate(report.get('candidates', [])):
            self.candidate_tree.insert('', 'end', tags=('stripe',) if i % 2 else (),
                values=(c['code'], c['name'], f"{c['score']:.1f}", f"{c['price']:.2f}",
                        f"{c.get('change', 0):+.2f}%", '、'.join(c.get('reasons', []))))
        self.report_text.delete('1.0', 'end')
        self.report_text.insert('end', '报告时间：' + report.get('generated_at', '未知') + '\n公告新闻尚未完整核验，历史回测尚未完成。\n\n' + report.get('ai_review', '') + '\n\n未入选原因：\n')
        self.report_text.insert('1.0', '问财状态：' + report.get('wencai_status', '此份旧报告未记录接口状态') + '\n')
        for c in report.get('rejected', []):
            self.report_text.insert('end', str(c.get('code', '')) + ' ' + c.get('name', '') + '：' + '、'.join(c.get('rejects') or [c.get('why', '')]) + '\n')

    def display_account(self):
        s = self.engine.snapshot()
        signature = json.dumps(s, sort_keys=True)
        if signature == self.account_signature:
            return
        self.account_signature = signature
        def value(x):
            return '待估值' if x is None else f'{x:,.2f}'
        for key, amount in [('cash', s['cash']), ('equity', s.get('equity', s['cash'] if not s['positions'] else None)),
                            ('return', s.get('return_pct')), ('drawdown', s.get('max_drawdown_pct'))]:
            self.metric_values[key].set(value(amount))
        self.position_tree.delete(*self.position_tree.get_children())
        for code, p in s['positions'].items():
            self.position_tree.insert('', 'end', values=(code, p['name'], p['quantity'], value(p['avg_cost']), value(p.get('mark'))))
        self.order_tree.delete(*self.order_tree.get_children())
        names = {'buy': '买入', 'sell': '卖出', 'cancel': '撤单'}
        statuses = {'pending': '待成交', 'filled': '已成交', 'cancelled': '已撤单', 'expired': '已失效', 'rejected': '未受理'}
        for o in reversed(s.get('orders', [])[-100:]):
            self.order_tree.insert('', 'end', tags=(o['status'],), values=(o['code'], names[o['side']], statuses[o['status']], o['created_at'][:19], o['reason']))

    def poll(self):
        try:
            while True:
                kind, value = self.init_events.get_nowait()
                if kind == 'engine':
                    self.engine = value
                    self.run_button.config(state='normal')
                    self.submit_button.config(state='normal')
                    self.apply_settings(value.settings)
                    self.save_button.config(state='normal')
                    self.status.set('就绪。监控当前暂停；点击“启动本机监控”处理已迁入的委托。')
                    cached = self.directory / 'report.json'
                    if cached.exists():
                        self.show_report(json.loads(cached.read_text(encoding='utf-8')))
                else:
                    self.status.set('初始化失败：' + value)
        except queue.Empty:
            pass
        if self.engine:
            if time.monotonic() - self.last_report_sync >= 300:
                self.last_report_sync = time.monotonic()
                self.engine.report()
            try:
                while True:
                    kind, value = self.engine.events.get_nowait()
                    if kind == 'report':
                        self.show_report(value)
                    elif kind == 'receipt':
                        self.log_text.insert('end', datetime.now(BEIJING).strftime('%H:%M:%S') + ' ' + value + '\n')
                        self.log_text.see('end')
                    else:
                        self.status.set(value)
            except queue.Empty:
                pass
            self.display_account()
        self.root.after(500, self.poll)

    def close(self):
        if self.engine and self.engine.running and not messagebox.askokcancel('退出软件', '退出后停止监控和模拟成交，委托保留在本机。确认退出？', parent=self.root):
            return
        if self.engine:
            self.engine.close()
        self.root.destroy()


def main():
    if getattr(sys, 'frozen', False):
        os.environ['PATH'] = str(Path(sys._MEIPASS) / 'node') + os.pathsep + os.environ.get('PATH', '')
    if os.name == 'nt':
        # PyExecJS's console helper must not flash a window during local scans.
        import functools
        import subprocess
        import execjs._external_runtime
        execjs._external_runtime.Popen = functools.partial(subprocess.Popen, creationflags=subprocess.CREATE_NO_WINDOW)
    parser = argparse.ArgumentParser()
    parser.add_argument('--smoke-test', action='store_true')
    parser.add_argument('--diagnostics-file')
    args = parser.parse_args()
    if args.diagnostics_file:
        result = {}
        try:
            import execjs
            result['javascript_runtime'] = execjs.eval('1+1') == 2
            from scripts.short_term_daily import _wencai_codes
            settings = load_settings()
            result['configured_fields'] = sorted(k for k, v in settings.items() if v)
            os.environ.update(settings)
            from quanti.data.tencent_quotes import fetch_snapshot
            started = time.perf_counter()
            quote = fetch_snapshot('603083')
            result.update(quote_ms=round((time.perf_counter()-started)*1000, 2), quote_time=quote['time'])
            write_json(Path(args.diagnostics_file), result)
            from quanti.wencai_client import configure_runtime
            configure_runtime()
            import importlib
            result['wencai_token_generated'] = bool(importlib.import_module('pywencai.headers').get_token())
            result['wencai_stock_count'] = len(_wencai_codes())
            from scripts.short_term_daily import WENCAI_STATUS
            result['wencai_status'] = WENCAI_STATUS
        except Exception as exc:
            result['error_type'] = type(exc).__name__
        write_json(Path(args.diagnostics_file), result)
        return
    if args.smoke_test:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = tk.Tk()
            root.withdraw()
            app = App(root, Path(tmp))
            deadline = time.monotonic() + 20
            while app.engine is None and time.monotonic() < deadline:
                root.update()
                time.sleep(.05)
            assert app.engine is not None
            assert not app.engine.running and not app.engine.state['trades']
            app.engine.close()
            root.destroy()
        return
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    mutex = kernel.CreateMutexW(None, False, 'Local\\QuantiDesktopPaper')
    if not mutex or ctypes.get_last_error() == 183:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo('Quanti', '软件已运行，请切换到现有窗口，避免同时执行两份账本。')
        root.destroy()
        return
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
