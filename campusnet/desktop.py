"""账号设置窗口与后台守护共用一个进程，所有 Tk 操作留在主线程。"""

from pathlib import Path
import queue
import threading

from .daemon import Daemon
from .runner import Runner
from .runtime import load_config, state_dir, write_json
from .settings import read_settings, save_credentials
from .singleton import ProcessLock
from .tray import ShowSignal, TrayIcon, icon_png, request_show


STATUS = {
    "starting": ("正在启动", "#64748b"),
    "checking": ("正在检查网络", "#2563eb"),
    "online": ("网络已连接", "#16834b"),
    "waiting": ("等待校园 Wi-Fi", "#64748b"),
    "retrying": ("正在重试登录", "#b7791f"),
    "needs_config": ("请填写账号密码", "#b7791f"),
    "stopped": ("后台已停止", "#64748b"),
}


class SettingsWindow:
    def __init__(self, config_path, log, signal, background=False):
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.config_path = Path(config_path)
        self.log, self.signal = log, signal
        self.actions = queue.Queue()
        self.stop_event = threading.Event()
        self.closing = False
        self.last_state = {}
        self.tray = None
        self.worker = None
        self.root = tk.Tk()
        self.root.withdraw()
        try:
            self._window_icon = tk.PhotoImage(data=icon_png())
            self.root.iconphoto(True, self._window_icon)
        except Exception:
            self._window_icon = None
        self.root.title("校园网 · 账号设置")
        self.root.resizable(False, False)
        self.root.configure(background="#f5f7fb")
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.report_callback_exception = self._callback_error
        self._build()
        self.daemon = Daemon(
            None, publish=self._publish, logger=log,
            reload_runner=lambda: Runner(load_config(self.config_path), logger=log),
        )
        try:
            username, password = read_settings(self.config_path)
            self.username.set(username)
            self.password.set(password)
        except (ValueError, OSError):
            self._feedback("配置读取失败，请检查配置文件后再保存。", error=True)
        self.tray = TrayIcon(lambda: self.actions.put("open"),
                             lambda: self.actions.put("exit"), logger=log)
        self.background = background

    def _build(self):
        tk, ttk = self.tk, self.ttk
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f5f7fb")
        style.configure("TLabel", background="#f5f7fb", foreground="#1e293b",
                        font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 21, "bold"))
        style.configure("Muted.TLabel", foreground="#64748b", font=("Microsoft YaHei UI", 9))
        style.configure("TEntry", padding=9, fieldbackground="white",
                        bordercolor="#cbd5e1", lightcolor="#cbd5e1", darkcolor="#cbd5e1")
        style.map("TEntry", bordercolor=[("focus", "#2563eb")])
        style.configure("TButton", padding=(14, 9), font=("Microsoft YaHei UI", 10))
        style.configure("Primary.TButton", background="#2563eb", foreground="white",
                        borderwidth=0)
        style.map("Primary.TButton", background=[("active", "#1d4ed8"), ("pressed", "#1e40af")])
        style.configure("TCheckbutton", background="#f5f7fb", font=("Microsoft YaHei UI", 9))
        style.map("TCheckbutton", background=[("active", "#f5f7fb")])

        content = ttk.Frame(self.root, padding=28)
        content.grid(sticky="nsew")
        content.columnconfigure(0, weight=1)
        ttk.Label(content, text="校园网", style="Title.TLabel").grid(sticky="w")
        ttk.Label(content, text="自动登录，安心保持连接", style="Muted.TLabel").grid(
            sticky="w", pady=(4, 22))

        self.status_text = tk.StringVar(value="正在启动")
        self.status_label = ttk.Label(content, textvariable=self.status_text, foreground="#64748b")
        self.status_label.grid(sticky="w")
        self.detail_text = tk.StringVar(value="仅在连接 tjus_wifi 时自动登录")
        ttk.Label(content, textvariable=self.detail_text, style="Muted.TLabel",
                  wraplength=370).grid(sticky="w", pady=(5, 20))
        ttk.Separator(content).grid(sticky="ew", pady=(0, 20))

        self.username, self.password = tk.StringVar(), tk.StringVar()
        self.show_password = tk.BooleanVar(value=False)
        ttk.Label(content, text="上网账号").grid(sticky="w", pady=(0, 6))
        self.username_entry = ttk.Entry(content, textvariable=self.username, width=36,
                                        font=("Microsoft YaHei UI", 10))
        self.username_entry.grid(sticky="ew", pady=(0, 16))
        ttk.Label(content, text="密码").grid(sticky="w", pady=(0, 6))
        self.password_entry = ttk.Entry(content, textvariable=self.password, show="●", width=36,
                                        font=("Microsoft YaHei UI", 10))
        self.password_entry.grid(sticky="ew")
        ttk.Checkbutton(content, text="显示密码", variable=self.show_password,
                        command=self._toggle_password).grid(sticky="w", pady=(9, 8))
        self.feedback_text = tk.StringVar(value="")
        self.feedback_label = ttk.Label(content, textvariable=self.feedback_text,
                                        style="Muted.TLabel", wraplength=370)
        self.feedback_label.grid(sticky="w", pady=(2, 12))
        buttons = ttk.Frame(content)
        buttons.grid(sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="保存并应用", style="Primary.TButton",
                   command=self.save).grid(row=0, column=0, sticky="ew", padx=(0, 10))
        ttk.Button(buttons, text="收起到托盘", command=self.hide).grid(row=0, column=1)
        ttk.Label(content, text="关闭窗口后，仍会在托盘中自动检查网络。",
                  style="Muted.TLabel").grid(sticky="w", pady=(20, 0))
        self.root.bind("<Return>", lambda _event: self.save())
        self.root.bind("<Escape>", lambda _event: self.hide())
        self.root.update_idletasks()
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry("{}x{}+{}+{}".format(width, height, x, y))

    def _toggle_password(self):
        self.password_entry.configure(show="" if self.show_password.get() else "●")

    def _feedback(self, text, error=False):
        self.feedback_text.set(text)
        self.feedback_label.configure(foreground="#b91c1c" if error else "#16834b")

    def _callback_error(self, kind, _value, _traceback):
        self.log("界面操作失败：" + kind.__name__, "error")
        self._feedback("操作未完成，请稍后重试。", error=True)

    def _publish(self, state):
        write_json(state_dir(self.config_path) / "status.json", state)
        self.actions.put(state)

    def save(self):
        try:
            save_credentials(self.config_path, self.username.get(), self.password.get())
        except ValueError as exc:
            self._feedback(str(exc), error=True)
            return
        except OSError:
            self._feedback("无法保存配置，请检查文件是否可写。", error=True)
            return
        self.daemon.request_reload()
        self._feedback("已保存，后台将使用新的账号配置。")
        self.log("账号配置已更新")

    def show(self):
        if self.closing:
            return
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()
        self.username_entry.focus_set()

    def hide(self):
        self.show_password.set(False)
        self._toggle_password()
        self.root.withdraw()

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.stop_event.set()
        self.daemon.request_reload()
        self.root.destroy()

    def _poll(self):
        if self.closing:
            return
        if self.signal.poll():
            self.show()
        while True:
            try:
                action = self.actions.get_nowait()
            except queue.Empty:
                break
            if action == "open":
                self.show()
            elif action == "exit":
                self.shutdown()
                return
            elif isinstance(action, dict):
                self.last_state = action
                title, color = STATUS.get(action.get("state"), ("正在运行", "#64748b"))
                self.status_text.set("●  " + title)
                self.status_label.configure(foreground=color)
                self.detail_text.set(action.get("message") or "仅在连接 tjus_wifi 时自动登录")
                self.tray.update("校园网 · " + title)
        self.root.after(200, self._poll)

    def run(self):
        try:
            self.tray.start()
            self.worker = threading.Thread(target=self.daemon.run, args=(self.stop_event,),
                                           name="campusnet-daemon", daemon=True)
            self.worker.start()
            self.root.after(200, self._poll)
            if not self.background:
                self.show()
            self.root.mainloop()
            return 0
        finally:
            self.stop_event.set()
            self.daemon.request_reload()
            if self.worker:
                self.worker.join(timeout=2)
            if self.tray:
                self.tray.stop()
            if not self.closing:
                self.root.destroy()


def run_app(config_path, log, background=False):
    lock = ProcessLock("campusnet-desktop")
    if not lock.acquire():
        if not background:
            request_show()
        return 0
    signal = ShowSignal()
    try:
        signal.create()
        return SettingsWindow(config_path, log, signal, background).run()
    finally:
        signal.close()
        lock.release()
