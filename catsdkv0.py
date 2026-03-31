#!/usr/bin/env python3
"""
CatSDK - A Ralph Loop clone in Tkinter using LM Studio as the API backend.
Iterative self-referential AI loop for interactive development.
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog
import threading
import json
import os
import time
import urllib.request
import urllib.error


DEFAULT_LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"
DEFAULT_MODEL = "local-model"
STATE_FILE = os.path.join(os.path.expanduser("~"), ".catsdk_loop_state.json")


class CatSDKApp:
    def __init__(self, root):
        self.root = root
        self.root.title("catsdk")
        self.root.geometry("960x720")
        self.root.minsize(800, 600)

        self.loop_running = False
        self.loop_thread = None
        self.iteration = 0
        self.max_iterations = 50
        self.completion_promise = "DONE"
        self.conversation_history = []
        self.stop_event = threading.Event()

        self._build_ui()
        self._load_state()

    def _build_ui(self):
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Export Chat...", command=self._export_chat)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.root.quit)
        menubar.add_cascade(label="File", menu=file_menu)
        self.root.config(menu=menubar)

        # --- LM Studio connection settings ---
        config_frame = ttk.LabelFrame(self.root, text="LM Studio Connection", padding=8)
        config_frame.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Label(config_frame, text="API URL:").grid(row=0, column=0, sticky=tk.W)
        self.url_var = tk.StringVar(value=DEFAULT_LM_STUDIO_URL)
        ttk.Entry(config_frame, textvariable=self.url_var, width=50).grid(row=0, column=1, padx=4)

        ttk.Label(config_frame, text="Model:").grid(row=0, column=2, sticky=tk.W, padx=(12, 0))
        self.model_var = tk.StringVar(value=DEFAULT_MODEL)
        ttk.Entry(config_frame, textvariable=self.model_var, width=20).grid(row=0, column=3, padx=4)

        ttk.Label(config_frame, text="Temp:").grid(row=0, column=4, sticky=tk.W, padx=(12, 0))
        self.temp_var = tk.StringVar(value="0.7")
        ttk.Entry(config_frame, textvariable=self.temp_var, width=5).grid(row=0, column=5, padx=4)

        self.connect_btn = ttk.Button(config_frame, text="Test Connection", command=self._test_connection)
        self.connect_btn.grid(row=0, column=6, padx=(12, 0))

        # --- Loop settings ---
        loop_frame = ttk.LabelFrame(self.root, text="Ralph Loop Settings", padding=8)
        loop_frame.pack(fill=tk.X, padx=8, pady=4)

        ttk.Label(loop_frame, text="Max Iterations:").grid(row=0, column=0, sticky=tk.W)
        self.max_iter_var = tk.StringVar(value="50")
        ttk.Entry(loop_frame, textvariable=self.max_iter_var, width=6).grid(row=0, column=1, padx=4)

        ttk.Label(loop_frame, text="Completion Promise:").grid(row=0, column=2, sticky=tk.W, padx=(12, 0))
        self.promise_var = tk.StringVar(value="DONE")
        ttk.Entry(loop_frame, textvariable=self.promise_var, width=20).grid(row=0, column=3, padx=4)

        ttk.Label(loop_frame, text="System Prompt:").grid(row=1, column=0, sticky=tk.NW, pady=(6, 0))
        self.system_prompt = tk.Text(loop_frame, height=3, width=80, wrap=tk.WORD)
        self.system_prompt.grid(row=1, column=1, columnspan=5, padx=4, pady=(6, 0), sticky=tk.EW)
        self.system_prompt.insert("1.0",
            "You are an iterative development assistant. Work on the task given to you. "
            "Each iteration, review your previous work and improve it. "
            "When the task is FULLY complete, output <promise>DONE</promise> to signal completion."
        )

        # --- Task prompt ---
        prompt_frame = ttk.LabelFrame(self.root, text="Task Prompt", padding=8)
        prompt_frame.pack(fill=tk.X, padx=8, pady=4)

        self.prompt_text = tk.Text(prompt_frame, height=4, wrap=tk.WORD)
        self.prompt_text.pack(fill=tk.X, expand=True)

        # --- Buttons ---
        btn_frame = ttk.Frame(self.root, padding=4)
        btn_frame.pack(fill=tk.X, padx=8)

        self.start_btn = ttk.Button(btn_frame, text="Start Loop", command=self._start_loop)
        self.start_btn.pack(side=tk.LEFT, padx=4)

        self.stop_btn = ttk.Button(btn_frame, text="Cancel Loop", command=self._cancel_loop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=4)

        self.single_btn = ttk.Button(btn_frame, text="Single Shot", command=self._single_shot)
        self.single_btn.pack(side=tk.LEFT, padx=4)

        self.clear_btn = ttk.Button(btn_frame, text="Clear Chat", command=self._clear_chat)
        self.clear_btn.pack(side=tk.LEFT, padx=4)

        self.status_var = tk.StringVar(value="Ready")
        self.iter_var = tk.StringVar(value="Iteration: 0 / 50")
        ttk.Label(btn_frame, textvariable=self.status_var, foreground="blue").pack(side=tk.RIGHT, padx=8)
        ttk.Label(btn_frame, textvariable=self.iter_var).pack(side=tk.RIGHT, padx=8)

        # --- Chat display ---
        chat_frame = ttk.LabelFrame(self.root, text="Conversation", padding=4)
        chat_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

        self.chat_display = scrolledtext.ScrolledText(chat_frame, wrap=tk.WORD, state=tk.DISABLED,
                                                       font=("Menlo", 11))
        self.chat_display.pack(fill=tk.BOTH, expand=True)

        self.chat_display.tag_configure("user", foreground="#2563eb", font=("Menlo", 11, "bold"))
        self.chat_display.tag_configure("assistant", foreground="#16a34a", font=("Menlo", 11, "bold"))
        self.chat_display.tag_configure("system", foreground="#9333ea", font=("Menlo", 11, "bold"))
        self.chat_display.tag_configure("error", foreground="#dc2626", font=("Menlo", 11, "bold"))
        self.chat_display.tag_configure("info", foreground="#ca8a04", font=("Menlo", 11, "italic"))
        self.chat_display.tag_configure("promise", foreground="#16a34a", background="#dcfce7",
                                         font=("Menlo", 12, "bold"))

    def _append_chat(self, role, text, tag=None):
        def _do():
            self.chat_display.configure(state=tk.NORMAL)
            if tag:
                self.chat_display.insert(tk.END, f"[{role}] ", tag)
            else:
                self.chat_display.insert(tk.END, f"[{role}] ")
            self.chat_display.insert(tk.END, text + "\n\n")
            self.chat_display.configure(state=tk.DISABLED)
            self.chat_display.see(tk.END)
        self.root.after(0, _do)

    def _update_status(self, text):
        self.root.after(0, lambda: self.status_var.set(text))

    def _update_iter(self):
        self.root.after(0, lambda: self.iter_var.set(f"Iteration: {self.iteration} / {self.max_iterations}"))

    def _set_buttons(self, running):
        def _do():
            state_on = tk.NORMAL if not running else tk.DISABLED
            state_stop = tk.NORMAL if running else tk.DISABLED
            self.start_btn.configure(state=state_on)
            self.single_btn.configure(state=state_on)
            self.stop_btn.configure(state=state_stop)
        self.root.after(0, _do)

    # --- LM Studio API (OpenAI-compatible) ---
    def _call_lm_studio(self, messages):
        url = self.url_var.get().strip()
        try:
            temp = float(self.temp_var.get())
        except ValueError:
            temp = 0.7

        payload = {
            "model": self.model_var.get().strip() or DEFAULT_MODEL,
            "messages": messages,
            "temperature": temp,
            "max_tokens": 4096,
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                      headers={"Content-Type": "application/json"}, method="POST")

        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        return result["choices"][0]["message"]["content"]

    def _test_connection(self):
        self._update_status("Testing connection...")

        def _do_test():
            try:
                reply = self._call_lm_studio([{"role": "user", "content": "Say hello in one word."}])
                self._append_chat("SYSTEM", f"LM Studio connected! Reply: {reply.strip()}", "system")
                self._update_status("Connected")
            except Exception as e:
                self._append_chat("ERROR", f"Connection failed: {e}", "error")
                self._update_status("Connection failed")

        threading.Thread(target=_do_test, daemon=True).start()

    # --- Loop logic ---
    def _check_promise(self, text):
        promise = self.promise_var.get().strip()
        return f"<promise>{promise}</promise>" in text

    def _run_loop(self, single=False):
        prompt = self.prompt_text.get("1.0", tk.END).strip()
        if not prompt:
            self._append_chat("ERROR", "No task prompt provided.", "error")
            self._set_buttons(False)
            return

        self.loop_running = True
        self.stop_event.clear()
        self._set_buttons(True)

        try:
            self.max_iterations = int(self.max_iter_var.get())
        except ValueError:
            self.max_iterations = 50

        self.completion_promise = self.promise_var.get().strip()

        # Build system message
        sys_prompt = self.system_prompt.get("1.0", tk.END).strip()
        if not self.conversation_history and sys_prompt:
            self.conversation_history = [{"role": "system", "content": sys_prompt}]

        max_runs = 1 if single else self.max_iterations

        while self.iteration < max_runs and not self.stop_event.is_set():
            self.iteration += 1
            self._update_iter()
            self._update_status(f"Running iteration {self.iteration}...")

            # Same prompt each iteration (Ralph Loop style)
            if self.iteration == 1:
                iter_prompt = prompt
            else:
                iter_prompt = (
                    f"[Iteration {self.iteration}/{self.max_iterations}] "
                    f"Continue working on the task. Review your previous output and improve it.\n\n"
                    f"Original task: {prompt}\n\n"
                    f"When fully complete, output <promise>{self.completion_promise}</promise>"
                )

            self.conversation_history.append({"role": "user", "content": iter_prompt})
            self._append_chat(f"USER (iter {self.iteration})", iter_prompt, "user")

            try:
                reply = self._call_lm_studio(self.conversation_history)
            except Exception as e:
                self._append_chat("ERROR", f"LM Studio API error: {e}", "error")
                self._update_status("Error - loop stopped")
                break

            self.conversation_history.append({"role": "assistant", "content": reply})
            self._append_chat(f"ASSISTANT (iter {self.iteration})", reply, "assistant")

            if self._check_promise(reply):
                self._append_chat("SYSTEM",
                    f"Completion promise detected! Loop finished after {self.iteration} iterations.",
                    "promise")
                self._update_status("Complete!")
                break

            # 2s delay between iterations, interruptible
            if not single and self.iteration < max_runs:
                for _ in range(20):
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.1)

        if self.stop_event.is_set():
            self._append_chat("SYSTEM", f"Loop cancelled at iteration {self.iteration}.", "info")
            self._update_status("Cancelled")
        elif self.iteration >= max_runs and not single:
            self._append_chat("SYSTEM", f"Max iterations ({self.max_iterations}) reached.", "info")
            self._update_status("Max iterations reached")

        self.loop_running = False
        self._set_buttons(False)
        self._save_state()

    def _start_loop(self):
        if self.loop_running:
            return
        self.iteration = 0
        self.conversation_history = []
        self.loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self.loop_thread.start()

    def _single_shot(self):
        if self.loop_running:
            return
        self.iteration = 0
        self.conversation_history = []
        self.loop_thread = threading.Thread(target=self._run_loop, args=(True,), daemon=True)
        self.loop_thread.start()

    def _cancel_loop(self):
        if self.loop_running:
            self.stop_event.set()
            self._update_status("Cancelling...")

    def _clear_chat(self):
        self.chat_display.configure(state=tk.NORMAL)
        self.chat_display.delete("1.0", tk.END)
        self.chat_display.configure(state=tk.DISABLED)
        self.conversation_history = []
        self.iteration = 0
        self._update_iter()
        self._update_status("Ready")

    # --- Persistence ---
    def _save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({"url": self.url_var.get(), "model": self.model_var.get()}, f)
        except Exception:
            pass

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    state = json.load(f)
                self.url_var.set(state.get("url", DEFAULT_LM_STUDIO_URL))
                self.model_var.set(state.get("model", DEFAULT_MODEL))
            except Exception:
                pass

    def _export_chat(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            title="Export Chat"
        )
        if path:
            try:
                with open(path, "w") as f:
                    f.write(self.chat_display.get("1.0", tk.END))
                self._append_chat("SYSTEM", f"Chat exported to {path}", "system")
            except Exception as e:
                self._append_chat("ERROR", f"Export failed: {e}", "error")


def main():
    root = tk.Tk()
    CatSDKApp(root)
    root.protocol("WM_DELETE_WINDOW", root.quit)
    root.mainloop()


if __name__ == "__main__":
    main()
