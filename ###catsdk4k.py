#!/usr/bin/env python3
"""
CatSDK - A Ralph Loop clone in Tkinter using LM Studio as the API backend.
Iterative self-referential AI loop with natural language action execution.

Supports two output modes from the LLM:
  PREFERRED — structured action tags:
    <file path="/path/file.ext">content</file>
    <shell>command</shell>
    <exec>python code</exec>
    <promise>DONE</promise>

  FALLBACK — standard markdown code blocks:
    ```bash blocks are executed as shell commands
    ```<lang> blocks with a detectable target path are written as files
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog
import threading
import json
import os
import re
import subprocess
import time
import urllib.request
import urllib.error


DEFAULT_LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"
DEFAULT_MODEL = "local-model"
STATE_FILE = os.path.join(os.path.expanduser("~"), ".catsdk_loop_state.json")

# Short, direct, example-heavy — local models need this
DEFAULT_SYSTEM_PROMPT = """\
You are CatSDK. You EXECUTE tasks by emitting action tags. CatSDK reads your tags and runs them.

NEVER explain. NEVER show instructions. NEVER say "here's how". Just emit tags.

TAGS:
<file path="/full/path/name.ext">
content
</file>

<shell>command</shell>

<exec>python code</exec>

<promise>DONE</promise>

EXAMPLE — user: "write hello.txt to /tmp"
<file path="/tmp/hello.txt">
Hello, world!
</file>
<promise>DONE</promise>

EXAMPLE — user: "create a python fibonacci script in ~/scripts"
<file path="~/scripts/fib.py">
def fib(n):
    a, b = 0, 1
    for _ in range(n):
        print(a)
        a, b = b, a + b

if __name__ == "__main__":
    fib(20)
</file>
<promise>DONE</promise>

EXAMPLE — user: "list files in /tmp"
<shell>ls -la /tmp</shell>
<promise>DONE</promise>

ALWAYS use <file path="..."> for creating files. ALWAYS end with <promise>DONE</promise>.\
"""

# ===== Regex patterns =====

# Primary: structured tags
RE_FILE = re.compile(
    r'<file\s+path\s*=\s*["\']?([^"\'<>\n]+?)["\']?\s*>(.*?)</file>',
    re.DOTALL,
)
RE_EXEC = re.compile(r'<exec>(.*?)</exec>', re.DOTALL)
RE_SHELL = re.compile(r'<shell>(.*?)</shell>', re.DOTALL)
RE_PROMISE = re.compile(r'<promise>(.*?)</promise>', re.DOTALL)

# Fallback: markdown code blocks
RE_CODEBLOCK = re.compile(r'```(\w*)\n(.*?)```', re.DOTALL)

# Path extraction from user prompt
RE_QUOTED_PATH = re.compile(r'["\']([^"\']*?/[^"\']+)["\']')
RE_BARE_PATH = re.compile(r'(?:^|\s)(/\S+)')
RE_TILDE_PATH = re.compile(r'(?:^|\s)(~/\S+)')
RE_FILENAME = re.compile(r'(?:^|\s)(\S+\.\w{1,6})(?:\s|$)')


def _strip_fences(text):
    """Strip markdown code fences so regex can find action tags inside them."""
    return re.sub(r'```[\w]*\n?', '', text)


def _extract_target_info(user_prompt):
    """Extract target directory and filename from the user's natural language prompt."""
    target_dir = None
    filename = None

    # Try quoted path first: "write foo to '/some/path'"
    m = RE_QUOTED_PATH.search(user_prompt)
    if m:
        p = m.group(1).strip()
        target_dir = os.path.expanduser(re.sub(r'\\(.)', r'\1', p))

    # Try bare /path
    if not target_dir:
        m = RE_TILDE_PATH.search(user_prompt) or RE_BARE_PATH.search(user_prompt)
        if m:
            p = m.group(1).strip().rstrip('.,;:!?')
            target_dir = os.path.expanduser(re.sub(r'\\(.)', r'\1', p))

    # Try to extract filename
    m = RE_FILENAME.search(user_prompt)
    if m:
        candidate = m.group(1)
        # Don't treat paths as filenames
        if '/' not in candidate:
            filename = candidate

    return target_dir, filename


class CatSDKApp:
    def __init__(self, root):
        self.root = root
        self.root.title("catsdk")
        self.root.geometry("1000x780")
        self.root.minsize(800, 600)

        self.loop_running = False
        self.loop_thread = None
        self.iteration = 0
        self.max_iterations = 50
        self.completion_promise = "DONE"
        self.conversation_history = []
        self.stop_event = threading.Event()
        self.auto_execute = tk.BooleanVar(value=True)
        self.current_user_prompt = ""

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

        # --- Connection ---
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

        ttk.Checkbutton(loop_frame, text="Auto-Execute Actions",
                         variable=self.auto_execute).grid(row=0, column=4, padx=(12, 0))

        ttk.Label(loop_frame, text="System Prompt:").grid(row=1, column=0, sticky=tk.NW, pady=(6, 0))
        self.system_prompt = tk.Text(loop_frame, height=4, width=80, wrap=tk.WORD)
        self.system_prompt.grid(row=1, column=1, columnspan=5, padx=4, pady=(6, 0), sticky=tk.EW)
        self.system_prompt.insert("1.0", DEFAULT_SYSTEM_PROMPT)

        # --- Task prompt ---
        prompt_frame = ttk.LabelFrame(self.root, text="Task Prompt (natural language)", padding=8)
        prompt_frame.pack(fill=tk.X, padx=8, pady=4)

        self.prompt_text = tk.Text(prompt_frame, height=3, wrap=tk.WORD)
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
        self.chat_display.tag_configure("action", foreground="#0891b2", font=("Menlo", 11, "bold"))
        self.chat_display.tag_configure("promise", foreground="#16a34a", background="#dcfce7",
                                         font=("Menlo", 12, "bold"))

    # --- UI helpers ---
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

    # --- URL normalization ---
    @staticmethod
    def _normalize_url(raw):
        url = raw.strip().rstrip("/")
        if not url.endswith("/v1/chat/completions"):
            for suffix in ("/v1/chat", "/v1"):
                if url.endswith(suffix):
                    url = url[: -len(suffix)]
                    break
            url += "/v1/chat/completions"
        return url

    # --- LM Studio API ---
    def _call_lm_studio(self, messages):
        url = self._normalize_url(self.url_var.get())
        try:
            temp = float(self.temp_var.get())
        except ValueError:
            temp = 0.7

        clean_messages = [m for m in messages if m.get("content", "").strip()]

        payload = {
            "model": self.model_var.get().strip() or DEFAULT_MODEL,
            "messages": clean_messages,
            "temperature": temp,
            "max_tokens": 4096,
            "stream": False,
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code} {e.reason}" + (f": {body}" if body else "")) from None

        choices = result.get("choices")
        if not choices:
            raise RuntimeError(f"No choices in response: {json.dumps(result)[:300]}")

        content = choices[0].get("message", {}).get("content", "")
        if not content:
            content = choices[0].get("text", "")
        return content

    def _test_connection(self):
        self._update_status("Testing connection...")

        def _do_test():
            try:
                resolved = self._normalize_url(self.url_var.get())
                self._append_chat("SYSTEM", f"Connecting to: {resolved}", "info")
                reply = self._call_lm_studio([{"role": "user", "content": "Say hello in one word."}])
                self._append_chat("SYSTEM", f"LM Studio connected! Reply: {reply.strip()}", "system")
                self._update_status("Connected")
            except Exception as e:
                self._append_chat("ERROR", f"Connection failed: {e}", "error")
                self._update_status("Connection failed")

        threading.Thread(target=_do_test, daemon=True).start()

    # ====================================================================
    #  ACTION ENGINE
    # ====================================================================

    def _sanitize_path(self, raw):
        """Unescape shell chars, expand ~, normalize."""
        unescaped = re.sub(r'\\(.)', r'\1', raw.strip().strip('"').strip("'"))
        return os.path.normpath(os.path.expanduser(unescaped))

    def _write_file(self, fpath, content, results):
        """Write content to fpath, create dirs, verify."""
        content = content.strip('\n')
        try:
            parent = os.path.dirname(fpath)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)

            if os.path.isfile(fpath):
                size = os.path.getsize(fpath)
                msg = f"FILE WRITTEN: {fpath} ({size} bytes) [verified]"
                results.append(("OK", msg))
                self._append_chat("ACTION", msg, "action")
            else:
                msg = f"FILE ANOMALY: {fpath} — write() ok but file missing"
                results.append(("ERR", msg))
                self._append_chat("ERROR", msg, "error")
        except Exception as e:
            msg = f"FILE FAILED: {fpath} — {e}"
            results.append(("ERR", msg))
            self._append_chat("ERROR", msg, "error")

    def _run_shell(self, cmd, results):
        """Run a shell command with timeout."""
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            out_parts = []
            if proc.stdout.strip():
                out_parts.append(proc.stdout.strip())
            if proc.stderr.strip():
                out_parts.append(f"(stderr) {proc.stderr.strip()}")
            out = "\n".join(out_parts) if out_parts else "(no output)"
            msg = f"SHELL [rc={proc.returncode}]: {cmd}\n{out}"
            results.append(("OK" if proc.returncode == 0 else "ERR", msg))
            self._append_chat("ACTION", msg, "action")
        except subprocess.TimeoutExpired:
            msg = f"SHELL TIMEOUT (30s): {cmd}"
            results.append(("ERR", msg))
            self._append_chat("ERROR", msg, "error")
        except Exception as e:
            msg = f"SHELL FAILED: {cmd} — {e}"
            results.append(("ERR", msg))
            self._append_chat("ERROR", msg, "error")

    def _run_exec(self, code, results):
        """Run Python code via subprocess."""
        try:
            proc = subprocess.run(["python3", "-c", code],
                                  capture_output=True, text=True, timeout=30)
            out_parts = []
            if proc.stdout.strip():
                out_parts.append(proc.stdout.strip())
            if proc.stderr.strip():
                out_parts.append(f"(stderr) {proc.stderr.strip()}")
            out = "\n".join(out_parts) if out_parts else "(no output)"
            msg = f"EXEC [rc={proc.returncode}]:\n{out}"
            results.append(("OK" if proc.returncode == 0 else "ERR", msg))
            self._append_chat("ACTION", msg, "action")
        except subprocess.TimeoutExpired:
            msg = "EXEC TIMEOUT (30s)"
            results.append(("ERR", msg))
            self._append_chat("ERROR", msg, "error")
        except Exception as e:
            msg = f"EXEC FAILED: {e}"
            results.append(("ERR", msg))
            self._append_chat("ERROR", msg, "error")

    def _execute_actions(self, text):
        """Parse and execute actions from LLM output. Tries structured tags first,
        falls back to markdown code blocks if the LLM ignored the tag format."""
        if not self.auto_execute.get():
            return []

        results = []
        used_primary = False

        # === PRIMARY: structured action tags ===
        # Strip markdown fences first so tags inside code blocks are visible
        cleaned = _strip_fences(text)

        for match in RE_FILE.finditer(cleaned):
            raw_path = match.group(1).strip()
            content = match.group(2)
            fpath = self._sanitize_path(raw_path)
            self._write_file(fpath, content, results)
            used_primary = True

        for match in RE_SHELL.finditer(cleaned):
            cmd = match.group(1).strip()
            self._run_shell(cmd, results)
            used_primary = True

        for match in RE_EXEC.finditer(cleaned):
            code = match.group(1).strip()
            self._run_exec(code, results)
            used_primary = True

        if used_primary:
            return results

        # === FALLBACK: LLM used markdown code blocks instead of tags ===
        self._append_chat("INFO",
            "LLM did not use action tags — falling back to code block execution.", "info")

        # Extract target dir and filename from the user's original prompt
        target_dir, target_filename = _extract_target_info(self.current_user_prompt)

        code_blocks = list(RE_CODEBLOCK.finditer(text))
        if not code_blocks:
            return results

        for cb in code_blocks:
            lang = cb.group(1).lower()
            body = cb.group(2)

            # --- bash/sh/shell blocks: execute as shell commands ---
            if lang in ("bash", "sh", "shell", "zsh"):
                # Execute each line (or the whole block) as a shell command
                # Some blocks are multi-line single commands, some are one-per-line
                full_cmd = body.strip()
                if full_cmd:
                    self._run_shell(full_cmd, results)
                continue

            # --- All other code blocks: try to write as a file ---
            if not body.strip():
                continue

            # Determine the output path
            fpath = None

            # Check for ```lang:filename syntax
            lang_file_match = re.match(r'(\w+):(.+)', cb.group(1))
            if lang_file_match:
                fname = lang_file_match.group(2).strip()
                if target_dir:
                    fpath = os.path.join(self._sanitize_path(target_dir), fname)
                else:
                    fpath = self._sanitize_path(fname)

            # Check for # filename: ... comment on first line
            if not fpath:
                first_line = body.strip().split('\n')[0]
                fm = re.match(r'^[#/]+\s*(?:filename|file|path)\s*:\s*(.+)', first_line, re.I)
                if fm:
                    fname = fm.group(1).strip()
                    if target_dir:
                        fpath = os.path.join(self._sanitize_path(target_dir), os.path.basename(fname))
                    else:
                        fpath = self._sanitize_path(fname)
                    # Remove the comment from content
                    body = '\n'.join(body.strip().split('\n')[1:])

            # Last resort: use target_dir + target_filename from user prompt
            if not fpath and target_dir and target_filename:
                fpath = os.path.join(self._sanitize_path(target_dir), target_filename)

            # If we still can't figure out where to write, try target_dir + lang extension
            if not fpath and target_dir and lang:
                ext_map = {
                    "python": ".py", "py": ".py", "javascript": ".js", "js": ".js",
                    "typescript": ".ts", "ts": ".ts", "rust": ".rs", "c": ".c",
                    "cpp": ".cpp", "java": ".java", "ruby": ".rb", "go": ".go",
                    "html": ".html", "css": ".css", "json": ".json", "yaml": ".yaml",
                    "yml": ".yml", "toml": ".toml", "xml": ".xml", "sql": ".sql",
                    "markdown": ".md", "md": ".md", "txt": ".txt", "text": ".txt",
                    "lua": ".lua", "swift": ".swift", "kotlin": ".kt", "r": ".r",
                }
                ext = ext_map.get(lang)
                if ext and target_filename:
                    fpath = os.path.join(self._sanitize_path(target_dir), target_filename)
                elif ext:
                    fpath = os.path.join(self._sanitize_path(target_dir), f"output{ext}")

            if fpath:
                self._write_file(fpath, body, results)
            else:
                self._append_chat("WARNING",
                    f"Code block ({lang or 'unknown'}) found but no target path could be determined. "
                    f"Skipped. Use a quoted path in your prompt, e.g.: write file.py to \"/some/path\"",
                    "info")

        return results

    # --- Promise check ---
    def _check_promise(self, text):
        promise = self.promise_var.get().strip()
        # Check both tag form and bare word
        return (f"<promise>{promise}</promise>" in text
                or f"\n{promise}\n" in f"\n{text}\n")

    # --- Build feedback ---
    def _build_action_feedback(self, results):
        if not results:
            return ""
        lines = ["[CatSDK Action Results]"]
        for status, msg in results:
            prefix = "OK" if status == "OK" else "FAILED"
            lines.append(f"  [{prefix}] {msg.split(chr(10))[0]}")
        return "\n".join(lines)

    # --- Loop logic ---
    def _run_loop(self, single=False):
        prompt = self.prompt_text.get("1.0", tk.END).strip()
        if not prompt:
            self._append_chat("ERROR", "No task prompt provided.", "error")
            self._set_buttons(False)
            return

        self.current_user_prompt = prompt
        self.loop_running = True
        self.stop_event.clear()
        self._set_buttons(True)

        try:
            self.max_iterations = int(self.max_iter_var.get())
        except ValueError:
            self.max_iterations = 50

        self.completion_promise = self.promise_var.get().strip()

        sys_prompt = self.system_prompt.get("1.0", tk.END).strip()
        if not self.conversation_history and sys_prompt:
            self.conversation_history = [{"role": "system", "content": sys_prompt}]

        max_runs = 1 if single else self.max_iterations
        last_action_feedback = ""

        while self.iteration < max_runs and not self.stop_event.is_set():
            self.iteration += 1
            self._update_iter()
            self._update_status(f"Running iteration {self.iteration}...")

            if self.iteration == 1:
                iter_prompt = prompt
            else:
                iter_prompt = (
                    f"[Iteration {self.iteration}/{self.max_iterations}] "
                    f"Continue working on the task. Review your previous output and improve it.\n\n"
                    f"Original task: {prompt}\n\n"
                )
                if last_action_feedback:
                    iter_prompt += last_action_feedback + "\n\n"
                iter_prompt += f"When fully complete, output <promise>{self.completion_promise}</promise>"

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

            action_results = self._execute_actions(reply)
            last_action_feedback = self._build_action_feedback(action_results)

            if self._check_promise(reply):
                self._append_chat("SYSTEM",
                    f"Completion promise detected! Loop finished after {self.iteration} iterations.",
                    "promise")
                self._update_status("Complete!")
                break

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
                json.dump({
                    "url": self._normalize_url(self.url_var.get()),
                    "model": self.model_var.get(),
                }, f)
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
