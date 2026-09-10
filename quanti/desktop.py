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
                    from scripts.short_term_daily import scan, ai_review
                    candidates, rejected = scan()
                    report = dict(generated_at=datetime.now(BEIJING).isoformat(), candidates=candidates,
                                  rejected=rejected, ai_review=ai_review(candidates), alerts=self.snapshot().get('alerts', []))
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
        self.rows = []
        self.account_signature = None
        self.last_report_sync = time.monotonic()
        root.title('Quanti 短线模拟盘 · 本机版')
        root.geometry('1120x760')
        root.minsize(900, 640)
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10))
        style.configure('Treeview', rowheight=30)
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 18, 'bold'))
        outer = ttk.Frame(root, padding=18)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='短线模拟盘', style='Title.TLabel').pack(anchor='w')
        ttk.Label(outer, text='本机保存委托与账本 · 推荐后由你确认 · 尚未连接券商实盘').pack(anchor='w', pady=(4, 12))
        toolbar = ttk.Frame(outer)
        toolbar.pack(fill='x')
        self.run_button = ttk.Button(toolbar, text='启动本机监控', command=self.toggle, state='disabled')
        self.run_button.pack(side='left', padx=(0, 8))
        ttk.Button(toolbar, text='更新云端候选', command=lambda: self.engine and self.engine.report()).pack(side='left', padx=4)
        ttk.Button(toolbar, text='在本机重新选股', command=lambda: self.engine and self.engine.report(True)).pack(side='left', padx=4)
        self.status = tk.StringVar(value='正在初始化本机引擎…')
        ttk.Label(outer, textvariable=self.status, wraplength=1050).pack(anchor='w', pady=10)
        self.metrics = tk.StringVar(value='正在读取账户…')
        ttk.Label(outer, textvariable=self.metrics, font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w', pady=5)
        trade = ttk.LabelFrame(outer, text='提交模拟委托', padding=12)
        trade.pack(fill='x', pady=10)
        self.side, self.code, self.fraction = tk.StringVar(value='买入'), tk.StringVar(), tk.StringVar(value='50%')
        for label, var, options in [('操作', self.side, ['买入', '卖出', '撤销待成交委托']), ('比例', self.fraction, ['25%', '50%', '100%'])]:
            ttk.Label(trade, text=label).pack(side='left', padx=5)
            ttk.Combobox(trade, textvariable=var, values=options, state='readonly', width=16).pack(side='left')
        ttk.Label(trade, text='股票代码').pack(side='left', padx=8)
        ttk.Entry(trade, textvariable=self.code, width=12).pack(side='left')
        self.submit_button = ttk.Button(trade, text='确认提交', command=self.submit, state='disabled')
        self.submit_button.pack(side='left', padx=12)
        ttk.Label(outer, text='买入比例按可用现金计算，卖出比例按该股当前股数计算。提交后等待有效新报价，未成交不会扣款。', wraplength=1050).pack(anchor='w')
        tabs = ttk.Notebook(outer)
        tabs.pack(fill='both', expand=True, pady=12)
        self.candidate_tree = self.table(tabs, '选股候选', ['代码', '名称', '分数', '报告价格', '量价理由'])
        self.candidate_tree.bind('<<TreeviewSelect>>', self.select_code)
        self.position_tree = self.table(tabs, '持仓', ['代码', '名称', '股数', '含费成本', '最近价格'])
        self.position_tree.bind('<<TreeviewSelect>>', self.select_code)
        self.order_tree = self.table(tabs, '委托与成交', ['代码', '操作', '状态', '提交时间', '说明'])
        self.order_tree.bind('<<TreeviewSelect>>', self.select_code)
        report_frame = ttk.Frame(tabs)
        tabs.add(report_frame, text='复盘与风险')
        self.report_text = tk.Text(report_frame, wrap='word', font=('Microsoft YaHei UI', 10))
        self.report_text.pack(fill='both', expand=True)
        self.report_text.insert('end', '公告新闻风险尚未完整核验，历史回测尚未完成。\n−5%是清仓提醒阈值，受T+1、跌停和行情延迟限制，不能保证最大亏损仅5%。\n软件关闭、电脑休眠或监控暂停时，不会检查或成交。\n')
        self.settings_frame = ttk.Frame(tabs, padding=12)
        tabs.add(self.settings_frame, text='接口设置')
        self.fields = {}
        fields = [('EMAIL_SENDER', 'QQ邮箱', False), ('EMAIL_PASSWORD', 'SMTP授权码', True),
                  ('LLM_PRIMARY_BASE_URL', '模型服务地址', False), ('LLM_PRIMARY_MODEL', '模型名称', False),
                  ('LLM_PRIMARY_API_KEY', '模型API密钥', True), ('WECOM_WEBHOOK_URL', '企业微信Webhook', True),
                  ('WENCAI_COOKIE', '问财Cookie', True)]
        for i, (key, label, secret) in enumerate(fields):
            ttk.Label(self.settings_frame, text=label).grid(row=i, column=0, sticky='w', padx=5, pady=5)
            var = self.fields[key] = tk.StringVar()
            ttk.Entry(self.settings_frame, textvariable=var, show='*' if secret else '', width=88).grid(row=i, column=1, sticky='ew', padx=5)
        ttk.Button(self.settings_frame, text='加密保存设置', command=self.save_settings).grid(row=len(fields), column=1, sticky='w', pady=10)
        ttk.Label(self.settings_frame, text='密钥只保存在当前Windows用户的加密设置中，不写入安装包或公开仓库。').grid(row=len(fields)+1, columnspan=2, sticky='w')
        root.protocol('WM_DELETE_WINDOW', self.close)
        threading.Thread(target=self.initialize, daemon=True).start()
        root.after(150, self.poll)

    def table(self, tabs, title, columns):
        frame = ttk.Frame(tabs)
        tabs.add(frame, text=title)
        tree = ttk.Treeview(frame, columns=columns, show='headings')
        for i, col in enumerate(columns):
            tree.heading(col, text=col)
            tree.column(col, width=120 if i < 4 else 430, minwidth=70)
        scroll = ttk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        return tree

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
        try:
            settings = {key: var.get().strip() for key, var in self.fields.items()}
            save_settings(settings, self.directory)
            if self.engine:
                self.engine.settings = settings
            self.status.set('设置已加密保存。')
        except Exception as exc:
            messagebox.showerror('保存失败', type(exc).__name__, parent=self.root)

    def show_report(self, report):
        self.candidate_tree.delete(*self.candidate_tree.get_children())
        for c in report.get('candidates', []):
            self.candidate_tree.insert('', 'end', values=(c['code'], c['name'], c['score'], c['price'], '、'.join(c.get('reasons', []))))
        self.report_text.delete('1.0', 'end')
        self.report_text.insert('end', '报告时间：' + report.get('generated_at', '未知') + '\n公告新闻尚未完整核验，历史回测尚未完成。\n\n' + report.get('ai_review', '') + '\n\n未入选原因：\n')
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
        self.metrics.set(f"可用现金 {value(s['cash'])}元     总资产 {value(s.get('equity'))}元     累计收益 {value(s.get('return_pct'))}%     采样最大回撤 {value(s.get('max_drawdown_pct'))}%")
        self.position_tree.delete(*self.position_tree.get_children())
        for code, p in s['positions'].items():
            self.position_tree.insert('', 'end', values=(code, p['name'], p['quantity'], value(p['avg_cost']), value(p.get('mark'))))
        self.order_tree.delete(*self.order_tree.get_children())
        names = {'buy': '买入', 'sell': '卖出', 'cancel': '撤单'}
        statuses = {'pending': '待成交', 'filled': '已成交', 'cancelled': '已撤单', 'expired': '已失效', 'rejected': '未受理'}
        for o in reversed(s.get('orders', [])[-100:]):
            self.order_tree.insert('', 'end', values=(o['code'], names[o['side']], statuses[o['status']], o['created_at'][:19], o['reason']))

    def poll(self):
        try:
            while True:
                kind, value = self.init_events.get_nowait()
                if kind == 'engine':
                    self.engine = value
                    self.run_button.config(state='normal')
                    self.submit_button.config(state='normal')
                    for key, var in self.fields.items():
                        var.set(value.settings.get(key, ''))
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
                        self.report_text.insert('end', '\n' + datetime.now(BEIJING).strftime('%H:%M:%S') + ' ' + value + '\n')
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
            result['wencai_stock_count'] = len(_wencai_codes())
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
