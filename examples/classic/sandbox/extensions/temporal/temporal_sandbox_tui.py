# mypy: ignore-errors
# standalone example with sys.path sibling imports that mypy cannot follow
"""Textual TUI for the Temporal Sandbox agent conversation client.

Sessions are managed entirely via Temporal — no filesystem persistence.
A central SessionManagerWorkflow tracks all active agent sessions.  The
TUI connects to it on startup to list, create, resume, and destroy sessions.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timezone
from pathlib import Path

from rich.markdown import Markdown
from rich.text import Text
from temporal_sandbox_agent import TurnState
from temporal_session_manager import (
    MANAGER_WORKFLOW_ID,
    BackendConfig,
    CreateSessionRequest,
    DaytonaBackendConfig,
    DockerBackendConfig,
    E2BBackendConfig,
    ForkSessionRequest,
    LocalBackendConfig,
    RenameRequest,
    SessionInfo,
    SessionManagerWorkflow,
    SwitchBackendRequest,
)
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.openai_agents import OpenAIAgentsPlugin
from temporalio.exceptions import WorkflowAlreadyStartedError
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    OptionList,
    Static,
    Tree,
)
from textual.widgets.option_list import Option

NEW_SESSION_ID = "__new__"
NEW_FROM_SNAPSHOT_ID = "__new_from_snapshot__"

SLASH_COMMANDS = [
    ("/title <name>", "Rename the current session"),
    ("/fork [title]", "Fork this session into a new one"),
    ("/switch [backend]", "Switch sandbox backend (daytona/local)"),
    ("/done", "Exit the session"),
]


class ToolDetailModal(ModalScreen):
    """Full-screen modal showing tool call command and output."""

    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="tool-modal"):
            with Vertical(id="tool-modal-box"):
                yield Static(self._title, id="tool-modal-title")
                with VerticalScroll(id="tool-modal-scroll"):
                    yield Static(self._body, id="tool-modal-body")

    def action_dismiss(self) -> None:
        self.app.pop_screen()


class ToolLine(Static):
    """A clickable one-line tool call summary in the chat flow."""

    def __init__(self, title: str, body: str, **kwargs) -> None:
        super().__init__(title, classes="tool-line", **kwargs)
        self._title = title
        self._body = body

    def on_click(self) -> None:
        self.app.push_screen(ToolDetailModal(self._title, self._body))


class ConversationApp(App):
    """Textual chat UI backed by Temporal workflows.

    On startup the app connects to the session manager, presents a session
    picker, and then enters the chat loop.  On exit the user chooses to
    keep the session alive (detach) or destroy it.
    """

    TITLE = "Sandbox Agent (live)"
    SUB_TITLE = "Temporal Workflow"

    CSS = """
    #chat {
        height: 1fr;
        border: round $accent;
        margin: 1 2;
        padding: 1 2;
        scrollbar-gutter: stable;
    }
    #chat > Static {
        margin: 0;
        padding: 0;
    }
    .tool-line {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    .tool-line:hover {
        background: $surface;
        color: $text;
    }
    #tool-modal {
        align: center middle;
    }
    #tool-modal-box {
        width: 90%;
        height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #tool-modal-title {
        height: 1;
        width: 1fr;
        text-style: bold;
        margin: 0 0 1 0;
    }
    #tool-modal-scroll {
        height: 1fr;
    }
    #tool-modal-body {
        height: auto;
    }
    #status-bar {
        height: 1;
        padding: 0 2;
        background: $surface;
        color: $text;
        layout: horizontal;
    }
    #liveness {
        width: auto;
    }
    #activity {
        width: auto;
        margin: 0 0 0 2;
    }
    Input {
        margin: 0 2 1 2;
    }
    #slash-menu {
        display: none;
        height: auto;
        max-height: 8;
        margin: 0 2;
        background: $surface;
        border: round $accent;
    }
    #session-picker {
        height: 1fr;
        margin: 1 2;
        border: round $accent;
        padding: 1;
    }
    #approval-bar {
        height: auto;
        margin: 0 2 1 2;
        layout: vertical;
    }
    #approval-label {
        width: 1fr;
        padding: 0 1 1 1;
    }
    #approval-buttons {
        height: auto;
        align-horizontal: center;
    }
    #approval-buttons Button {
        margin: 0 1;
    }
    #exit-bar {
        height: auto;
        margin: 0 2 1 2;
        layout: vertical;
    }
    #exit-label {
        width: 1fr;
        padding: 0 1 1 1;
    }
    #exit-buttons {
        height: auto;
        align-horizontal: center;
    }
    #exit-buttons Button {
        margin: 0 1;
    }
    #fork-bar {
        height: auto;
        margin: 0 2 1 2;
        layout: vertical;
    }
    #fork-label {
        width: 1fr;
        padding: 0 1 1 1;
    }
    #fork-buttons {
        height: auto;
        align-horizontal: center;
    }
    #fork-buttons Button {
        margin: 0 1;
    }
    #snapshot-picker {
        height: 1fr;
        margin: 1 2;
        border: round $accent;
        padding: 1;
    }
    #backend-picker {
        height: auto;
        margin: 1 2;
        layout: vertical;
    }
    #backend-label {
        width: 1fr;
        padding: 0 1 1 1;
    }
    #backend-buttons {
        height: auto;
        align-horizontal: center;
    }
    #backend-buttons Button {
        margin: 0 1;
    }
    #workspace-picker {
        height: auto;
        margin: 1 2;
        layout: vertical;
    }
    #workspace-label {
        width: 1fr;
        padding: 0 1 1 1;
    }
    #workspace-input {
        margin: 0 2 1 2;
    }
    #workspace-buttons {
        height: auto;
        align-horizontal: center;
    }
    #workspace-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit_graceful", "Quit", priority=True),
    ]

    def __init__(
        self,
        *,
        workflow_cls: type,
        task_queue: str,
        cwd: str,
    ) -> None:
        super().__init__()
        self._workflow_cls = workflow_cls
        self._task_queue = task_queue
        self._cwd = cwd
        self._handle: WorkflowHandle | None = None
        self._manager_handle: WorkflowHandle | None = None
        self._temporal_client: Client | None = None
        self._current_workflow_id: str | None = None
        self._poll_timer = None
        self._last_paused: bool = False
        self._pending_fork_title: str | None = None
        self._cached_sessions: list[SessionInfo] = []
        self._current_backend: str = "daytona"
        self._current_turn_id: int = 0
        self._pending_backend_action: str = "new_session"  # "new_session" or "switch"

    async def _backfill_snapshot_ids(self, sessions: list[SessionInfo]) -> None:
        """Query each workflow's live snapshot ID concurrently.

        Fills in ``snapshot_id`` on SessionInfo objects that don't already
        have one (e.g. sessions created fresh, before any fork/persist).
        """
        assert self._temporal_client is not None
        missing = [s for s in sessions if not s.snapshot_id]
        if not missing:
            return

        async def _fetch(s: SessionInfo) -> None:
            try:
                handle = self._temporal_client.get_workflow_handle(s.workflow_id)  # type: ignore[union-attr]
                sid = await handle.query(self._workflow_cls.get_snapshot_id)
                if sid:
                    s.snapshot_id = sid
            except Exception:
                pass

        await asyncio.gather(*[_fetch(s) for s in missing])

    # -- Status helpers -----------------------------------------------------

    def _set_liveness(self, text: str | Text) -> None:
        """Update the persistent liveness indicator (Active / Paused)."""
        self.query_one("#liveness", Static).update(text)

    def _set_activity(self, text: str | Text = "") -> None:
        """Update the transient activity indicator (Thinking / Approval / Error).

        Pass empty string to clear."""
        self.query_one("#activity", Static).update(text)

    # -- Chat helpers -------------------------------------------------------

    def _chat_write(self, content) -> None:
        """Append a renderable to the chat scroll area."""
        chat = self.query_one("#chat", VerticalScroll)
        chat.mount(Static(content))
        chat.scroll_end(animate=False)

    def _chat_clear(self) -> None:
        """Remove all children from the chat scroll area."""
        chat = self.query_one("#chat", VerticalScroll)
        chat.remove_children()

    @staticmethod
    def _tool_call_title(tc) -> str:
        """Format a one-line title for a tool call Collapsible."""
        icon = "\u2713" if tc.status == "completed" else "\u23f3"
        full_text = tc.arguments
        try:
            args = json.loads(tc.arguments)
            if "commands" in args:
                cmds = args["commands"]
                full_text = "; ".join(cmds) if cmds else "(empty)"
            elif "command" in args:
                full_text = args["command"]
        except (json.JSONDecodeError, TypeError):
            pass
        lines = full_text.split("\n")
        first_line = lines[0]
        if len(first_line) > 80:
            first_line = first_line[:77] + "..."
        extra = len(lines) - 1
        suffix = f"  [... +{extra} lines]" if extra > 0 else ""
        return f"{icon} {tc.tool_name}: {first_line}{suffix}"

    @staticmethod
    def _tool_call_body(tc) -> str:
        """Format the expanded body of a tool call Collapsible."""
        parts = []
        try:
            args = json.loads(tc.arguments)
            parts.append(json.dumps(args, indent=2))
        except (json.JSONDecodeError, TypeError):
            parts.append(tc.arguments)
        if tc.status == "completed":
            output = tc.output or "(empty)"
            parts.append(f"\n--- output ---\n{output}")
        elif tc.status == "running":
            parts.append("\n\u23f3 Running...")
        else:
            parts.append("\n\u23f3 Pending...")
        return "\n".join(parts)

    async def _render_live_tool_calls(self, state: TurnState) -> None:
        """Create or update ToolLine widgets for live tool calls."""
        chat = self.query_one("#chat", VerticalScroll)
        for tc in state.tool_calls:
            widget_id = "tc_" + "".join(c if c.isalnum() else "_" for c in tc.call_id)
            title = self._tool_call_title(tc)
            body = self._tool_call_body(tc)
            existing = self.query(f"#{widget_id}")
            if existing:
                line = existing.first(ToolLine)
                line.update(title)
                line._body = body
            else:
                await chat.mount(ToolLine(title, body, id=widget_id))
        chat.scroll_end(animate=False)

    # -- Layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Tree("Sessions", id="session-picker")
        yield Tree("Pick a source session", id="snapshot-picker")
        with Vertical(id="backend-picker"):
            yield Static("Choose sandbox backend:", id="backend-label")
            with Horizontal(id="backend-buttons"):
                yield Button("Daytona (cloud)", id="btn-backend-daytona", variant="primary")
                yield Button("Docker", id="btn-backend-docker", variant="primary")
                yield Button("E2B (cloud)", id="btn-backend-e2b", variant="primary")
                yield Button("Local (unix)", id="btn-backend-local", variant="warning")
        with Vertical(id="workspace-picker"):
            yield Static(
                "Workspace root (agent files will be created here):",
                id="workspace-label",
            )
            yield Input(id="workspace-input", placeholder="/absolute/path/to/workspace")
            with Horizontal(id="workspace-buttons"):
                yield Button("Accept", id="btn-workspace-accept", variant="success")
                yield Button("Cancel", id="btn-workspace-cancel", variant="error")
        yield VerticalScroll(id="chat")
        with Vertical(id="approval-bar"):
            yield Static("", id="approval-label")
            with Horizontal(id="approval-buttons"):
                yield Button("Approve", id="btn-approve", variant="success")
                yield Button("Deny", id="btn-deny", variant="error")
        with Vertical(id="fork-bar"):
            yield Static("", id="fork-label")
            with Horizontal(id="fork-buttons"):
                yield Button("Copy snapshot", id="btn-fork-copy", variant="success")
                yield Button("Share snapshot", id="btn-fork-share", variant="warning")
        with Vertical(id="exit-bar"):
            yield Static("Keep this session alive for later?", id="exit-label")
            with Horizontal(id="exit-buttons"):
                yield Button("Keep Alive", id="btn-keep", variant="success")
                yield Button("Destroy", id="btn-destroy", variant="error")
        yield OptionList(id="slash-menu")
        yield Input(placeholder="Connecting to Temporal...", disabled=True, id="chat-input")
        with Horizontal(id="status-bar"):
            yield Static("Connecting...", id="liveness")
            yield Static("", id="activity")
        yield Footer()

    async def on_mount(self) -> None:
        # Start in session-picker mode: hide chat UI
        self.query_one("#chat").display = False
        self.query_one("#chat-input", Input).display = False
        self.query_one("#approval-bar").display = False
        self.query_one("#fork-bar").display = False
        self.query_one("#exit-bar").display = False
        self.query_one("#snapshot-picker").display = False
        self.query_one("#backend-picker").display = False
        self.query_one("#workspace-picker").display = False
        self._init_temporal()

    # -- Phase 1: Connect to Temporal and populate session picker -----------

    @work
    async def _init_temporal(self) -> None:
        tree = self.query_one("#session-picker", Tree)

        try:
            plugin = OpenAIAgentsPlugin()
            self._temporal_client = await Client.connect(
                "localhost:7233",
                plugins=[plugin],
            )
        except Exception as e:
            self._set_liveness(f"Connection failed: {e}")
            return

        # Ensure the session manager singleton is running
        try:
            self._manager_handle = await self._temporal_client.start_workflow(
                SessionManagerWorkflow.run,
                id=MANAGER_WORKFLOW_ID,
                task_queue=self._task_queue,
            )
        except WorkflowAlreadyStartedError:
            self._manager_handle = self._temporal_client.get_workflow_handle(MANAGER_WORKFLOW_ID)

        # Query existing sessions, backfill live snapshot IDs, and build the tree
        sessions = await self._manager_handle.query(SessionManagerWorkflow.list_sessions)
        await self._backfill_snapshot_ids(sessions)
        self._populate_session_tree(tree, sessions)

        self._set_liveness("Select a session")
        tree.root.expand_all()
        tree.focus()

    # Distinct background colors for snapshot badges — chosen for
    # readability on both light and dark terminal themes.
    _SNAPSHOT_COLORS = [
        ("on dark_green", "bold white"),
        ("on dark_blue", "bold white"),
        ("on dark_magenta", "bold white"),
        ("on dark_cyan", "bold white"),
        ("on dark_red", "bold white"),
        ("on yellow", "bold black"),
        ("on dodger_blue2", "bold white"),
        ("on deep_pink4", "bold white"),
        ("on orange3", "bold black"),
        ("on chartreuse4", "bold white"),
    ]

    def _populate_session_tree(self, tree: Tree, sessions: list) -> None:
        """Build a nested tree from sessions with parent/child relationships."""
        tree.root.remove_children()
        self._cached_sessions = list(sessions)

        # Index sessions by workflow_id and group children by parent
        by_id: dict[str, object] = {}
        children_of: dict[str | None, list] = {None: []}
        for s in sessions:
            by_id[s.workflow_id] = s
            parent = s.parent_workflow_id
            # If the parent was destroyed, treat this as a root session
            if parent and parent not in {si.workflow_id for si in sessions}:
                parent = None
            children_of.setdefault(parent, [])
            children_of[parent].append(s)

        # Build a stable color mapping for unique snapshot IDs
        unique_snap_ids: list[str] = []
        seen: set[str] = set()
        for s in sessions:
            if s.snapshot_id and s.snapshot_id not in seen:
                unique_snap_ids.append(s.snapshot_id)
                seen.add(s.snapshot_id)
        snap_color_map: dict[str, tuple[str, str]] = {}
        for i, sid in enumerate(unique_snap_ids):
            snap_color_map[sid] = self._SNAPSHOT_COLORS[i % len(self._SNAPSHOT_COLORS)]

        def _format_label(s: SessionInfo) -> Text:
            utc_time = s.created_at.replace(tzinfo=timezone.utc)
            created = utc_time.astimezone().strftime("%Y-%m-%d %I:%M %p")

            label = Text()
            label.append(f"{s.title}  ")
            label.append(f"({created})", style="dim")

            if s.backend:
                label.append(f"  [{s.backend.type}]", style="bold dim")

            if s.snapshot_id:
                short = s.snapshot_id[:8]
                bg, fg = snap_color_map[s.snapshot_id]
                label.append("  ")
                label.append(f" {short} ", style=f"{fg} {bg}")

            return label

        def _add_children(parent_node, parent_id: str | None) -> None:
            for s in children_of.get(parent_id, []):
                label = _format_label(s)
                if children_of.get(s.workflow_id):
                    branch = parent_node.add(label, data=s.workflow_id)
                    _add_children(branch, s.workflow_id)
                else:
                    parent_node.add_leaf(label, data=s.workflow_id)

        _add_children(tree.root, None)
        tree.root.add_leaf("+ New Session", data=NEW_SESSION_ID)
        if sessions:
            tree.root.add_leaf("+ New from snapshot...", data=NEW_FROM_SNAPSHOT_ID)

    # -- Session selection --------------------------------------------------

    async def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        node_data = event.node.data
        if node_data is None:
            return

        tree_id = event.node.tree.id

        # Handle snapshot picker selection (choosing source for "new from snapshot")
        if tree_id == "snapshot-picker":
            self.query_one("#snapshot-picker").display = False
            self._create_session_from_snapshot(str(node_data))
            return

        # Handle main session picker
        self.query_one("#session-picker").display = False

        if node_data == NEW_SESSION_ID:
            self._pending_backend_action = "new_session"
            self._show_backend_picker()
            return
        elif node_data == NEW_FROM_SNAPSHOT_ID:
            self._show_snapshot_source_picker()
        else:
            self._resume_session(str(node_data))

    def _show_backend_picker(self) -> None:
        """Show the backend selection buttons."""
        self.query_one("#backend-picker").display = True
        self._set_liveness("Choose a sandbox backend")

    def _on_backend_chosen(self, backend: BackendConfig) -> None:
        """Dispatch after the backend picker completes."""
        if self._pending_backend_action == "switch":
            self._switch_backend(backend)
        elif self._pending_backend_action == "fork":
            self._fork_session(self._pending_fork_title, backend)
            self._pending_fork_title = None
        else:
            self._create_new_session(backend=backend)

    def _show_snapshot_source_picker(self) -> None:
        """Show a sub-tree of sessions to pick a snapshot source from."""
        tree = self.query_one("#snapshot-picker", Tree)
        tree.root.remove_children()
        for s in self._cached_sessions:
            utc_time = s.created_at.replace(tzinfo=timezone.utc)
            created = utc_time.astimezone().strftime("%Y-%m-%d %I:%M %p")
            tree.root.add_leaf(f"{s.title}  ({created})", data=s.workflow_id)
        tree.root.expand_all()
        tree.display = True
        self._set_liveness("Pick a session to start from")
        tree.focus()

    @work
    async def _create_new_session(
        self,
        backend: BackendConfig | None = None,
    ) -> None:
        if backend is None:
            backend = DaytonaBackendConfig()
        self.query_one("#chat").display = True
        self._set_liveness("Creating session...")
        self._chat_write(Text(f"Starting new {backend.type} session...\n", style="yellow"))

        assert self._manager_handle is not None
        assert self._temporal_client is not None
        try:
            workflow_id: str = await self._manager_handle.execute_update(
                SessionManagerWorkflow.create_session,
                CreateSessionRequest(cwd=self._cwd, backend=backend),
            )
        except Exception as e:
            self._chat_write(Text(f"Failed to create session: {e}", style="bold red"))
            self._set_liveness("Error")
            return

        self._current_workflow_id = workflow_id
        self._current_backend = backend.type
        self._handle = self._temporal_client.get_workflow_handle(workflow_id)
        self._current_turn_id = 0
        self._set_session_title(f"Session {workflow_id[-8:]}")

        self._chat_write(Text(f"Session started: {workflow_id}\n", style="green"))
        self._switch_to_chat()

    @work
    async def _create_session_from_snapshot(self, source_workflow_id: str) -> None:
        self.query_one("#chat").display = True
        self._set_liveness("Creating session from snapshot...")
        self._chat_write(Text("Creating session from existing snapshot...\n", style="yellow"))

        assert self._manager_handle is not None
        assert self._temporal_client is not None
        try:
            workflow_id: str = await self._manager_handle.execute_update(
                SessionManagerWorkflow.fork_session,
                ForkSessionRequest(source_workflow_id=source_workflow_id),
            )
        except Exception as e:
            self._chat_write(Text(f"Failed to create session: {e}", style="bold red"))
            self._set_liveness("Error")
            return

        self._current_workflow_id = workflow_id
        self._handle = self._temporal_client.get_workflow_handle(workflow_id)
        self._current_turn_id = 0
        self._set_session_title(f"Session {workflow_id[-8:]}")

        self._chat_write(Text(f"Session started from snapshot: {workflow_id}\n", style="green"))
        self._switch_to_chat()

    @work
    async def _resume_session(self, workflow_id: str) -> None:
        self.query_one("#chat").display = True
        self._set_liveness("Resuming session...")

        assert self._temporal_client is not None
        self._current_workflow_id = workflow_id
        self._handle = self._temporal_client.get_workflow_handle(workflow_id)

        # Sync turn_id so we don't mistake prior "complete" as a new response
        try:
            state = await self._handle.query(self._workflow_cls.get_turn_state)
            self._current_turn_id = state.turn_id
        except Exception:
            self._current_turn_id = 0

        # Replay conversation history from the workflow
        try:
            history: list[dict] = await self._handle.query(self._workflow_cls.get_history)
            self._render_history(history)
        except Exception as e:
            self._chat_write(Text(f"Could not load history: {e}", style="yellow"))

        # Look up the session title and backend from the manager
        assert self._manager_handle is not None
        try:
            sessions = await self._manager_handle.query(SessionManagerWorkflow.list_sessions)
            for s in sessions:
                if s.workflow_id == workflow_id:
                    self._set_session_title(s.title)
                    self._current_backend = s.backend.type
                    break
        except Exception:
            self._set_session_title(workflow_id[-8:])

        self._chat_write(Text(f"Resumed session: {workflow_id}\n", style="green"))
        self._switch_to_chat()

    def _set_session_title(self, title: str) -> None:
        """Update the header to show the active session title."""
        self.sub_title = title

    def _switch_to_chat(self) -> None:
        """Transition from session picker to chat mode."""
        input_w = self.query_one("#chat-input", Input)
        input_w.display = True
        input_w.placeholder = "Type a message, or / for commands..."
        input_w.disabled = False
        input_w.focus()
        self._set_liveness(Text(f"● Active [{self._current_backend}]", style="green"))
        self._set_activity()
        self._poll_timer = self.set_interval(3, self._poll_liveness)

    def _render_history(self, history: list[dict]) -> None:
        """Replay conversation history returned by the workflow query."""
        for entry in history:
            if entry.get("role") == "user":
                self._chat_write(Text(f"> {entry['content']}", style="bold cyan"))
            elif entry.get("role") == "assistant":
                self._chat_write(Markdown(entry["content"]))
        if history:
            self._chat_write(Text("--- session restored ---\n", style="dim"))

    # -- Liveness polling ---------------------------------------------------

    @work(exclusive=True, group="liveness")
    async def _poll_liveness(self) -> None:
        """Query the workflow's paused state and update the status bar."""
        if self._handle is None:
            return
        try:
            paused = await self._handle.query(self._workflow_cls.is_paused)
        except Exception:
            return
        was_paused = self._last_paused
        self._last_paused = paused
        if paused:
            self._set_liveness(Text(f"● Paused [{self._current_backend}]", style="yellow"))
        else:
            self._set_liveness(Text(f"● Active [{self._current_backend}]", style="green"))
            # Session just came back — promote "Resuming..." to "Thinking..."
            if was_paused:
                self._set_activity(Text("Thinking...", style="cyan"))

    # -- Slash-command autocomplete -------------------------------------------

    def _accept_slash_highlighted(self) -> None:
        """Tab-accept: insert highlighted command, dismiss menu."""
        menu = self.query_one("#slash-menu", OptionList)
        input_w = self.query_one("#chat-input", Input)
        if menu.highlighted is None:
            return
        option = menu.get_option_at_index(menu.highlighted)
        cmd = option.id
        menu.display = False
        self._slash_menu_open = False
        input_w.value = cmd + " " if cmd != "/done" else "/done"
        input_w.focus()
        self.set_timer(0.05, lambda: setattr(input_w, "cursor_position", len(input_w.value)))

    _slash_menu_open: bool = False

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "chat-input":
            return
        menu = self.query_one("#slash-menu", OptionList)
        val = event.value
        if not val.startswith("/") or " " in val:
            menu.display = False
            self._slash_menu_open = False
            return
        # Filter commands matching the typed prefix
        prefix = val.lower()
        matches = [(cmd, desc) for cmd, desc in SLASH_COMMANDS if cmd.split()[0].startswith(prefix)]
        menu.clear_options()
        for cmd, desc in matches:
            menu.add_option(Option(f"{cmd}  — {desc}", id=cmd.split()[0]))
        menu.display = bool(matches)
        self._slash_menu_open = bool(matches)
        if matches:
            menu.highlighted = 0

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._accept_slash_highlighted()

    async def on_key(self, event) -> None:
        if not self._slash_menu_open:
            return
        menu = self.query_one("#slash-menu", OptionList)
        if event.key == "up":
            if menu.highlighted is not None and menu.highlighted > 0:
                menu.highlighted -= 1
            event.prevent_default()
            event.stop()
        elif event.key == "down":
            if menu.highlighted is not None:
                menu.highlighted += 1
            event.prevent_default()
            event.stop()
        elif event.key == "tab":
            self._accept_slash_highlighted()
            event.prevent_default()
            event.stop()
        elif event.key == "escape":
            menu.display = False
            self._slash_menu_open = False
            event.prevent_default()
            event.stop()

    # -- Phase 2: Chat ------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "workspace-input":
            # Treat Enter on workspace input as clicking Accept
            self.query_one("#workspace-picker").display = False
            raw = event.value.strip()
            workspace_root = Path(raw) if raw else Path(self._cwd) / "workspace"
            self._on_backend_chosen(LocalBackendConfig(workspace_root=workspace_root))
            return

        self.query_one("#slash-menu", OptionList).display = False
        self._slash_menu_open = False

        message = event.value.strip()
        if not message:
            return

        input_w = self.query_one("#chat-input", Input)
        input_w.value = ""

        # Meta-command: /title <name>
        if message.startswith("/title "):
            new_title = message[len("/title ") :].strip()
            if new_title:
                self._rename_session(new_title)
            return

        # Meta-command: /fork [optional title] — pick backend then fork
        if message == "/fork" or message.startswith("/fork "):
            self._pending_fork_title = message[len("/fork") :].strip() or None
            self._pending_backend_action = "fork"
            self._show_backend_picker()
            return

        # Meta-command: /switch — interactively switch sandbox backend
        if message == "/switch":
            self._pending_backend_action = "switch"
            self._show_backend_picker()
            return

        # Exit flow
        if message.lower() == "/done":
            self._show_exit_prompt()
            return

        self._chat_write(Text(f"> {message}", style="bold cyan"))
        input_w.disabled = True
        if self._last_paused:
            self._set_activity(Text("Resuming...", style="cyan"))
        else:
            self._set_activity(Text("Thinking...", style="cyan"))
        self._send_message(message)

    @work
    async def _rename_session(self, new_title: str) -> None:
        assert self._manager_handle is not None
        assert self._current_workflow_id is not None
        try:
            await self._manager_handle.signal(
                SessionManagerWorkflow.rename_session,
                RenameRequest(workflow_id=self._current_workflow_id, title=new_title),
            )
            self._set_session_title(new_title)
            self._chat_write(Text(f"Session renamed to: {new_title}", style="green"))
        except Exception as e:
            self._chat_write(Text(f"Rename failed: {e}", style="bold red"))

    @work
    async def _fork_session(
        self,
        title: str | None,
        backend: BackendConfig | None = None,
    ) -> None:
        input_w = self.query_one("#chat-input", Input)

        assert self._manager_handle is not None
        assert self._current_workflow_id is not None

        input_w.disabled = True
        self._set_activity(Text("Forking...", style="cyan"))
        self._chat_write(Text("\nForking session...", style="yellow"))

        try:
            new_workflow_id: str = await self._manager_handle.execute_update(
                SessionManagerWorkflow.fork_session,
                ForkSessionRequest(
                    source_workflow_id=self._current_workflow_id,
                    title=title,
                    target_backend=backend,
                ),
            )
        except Exception as e:
            self._chat_write(Text(f"Fork failed: {e}", style="bold red"))
            self._set_activity(Text("Error", style="red"))
            input_w.disabled = False
            input_w.focus()
            return

        # Switch to the forked session
        self._current_workflow_id = new_workflow_id
        if backend is not None:
            self._current_backend = backend.type
        self._handle = self._temporal_client.get_workflow_handle(new_workflow_id)
        self._current_turn_id = 0

        # Resolve the title that was assigned
        fork_title = title or new_workflow_id[-8:]
        try:
            sessions = await self._manager_handle.query(SessionManagerWorkflow.list_sessions)
            for s in sessions:
                if s.workflow_id == new_workflow_id:
                    fork_title = s.title
                    break
        except Exception:
            pass

        self._set_session_title(fork_title)
        self._chat_write(Text(f"Forked! Now in: {fork_title} ({new_workflow_id})", style="green"))
        self._set_liveness(Text(f"● Active [{self._current_backend}]", style="green"))
        self._set_activity()
        input_w.disabled = False
        input_w.focus()

    @work
    async def _switch_backend(self, backend: BackendConfig) -> None:
        input_w = self.query_one("#chat-input", Input)

        assert self._manager_handle is not None
        assert self._current_workflow_id is not None

        input_w.disabled = True
        self._set_activity(Text("Switching backend...", style="cyan"))
        self._chat_write(Text(f"\nSwitching to {backend.type}...", style="yellow"))

        try:
            await self._manager_handle.execute_update(
                SessionManagerWorkflow.switch_backend,
                SwitchBackendRequest(
                    source_workflow_id=self._current_workflow_id,
                    target_backend=backend,
                ),
            )
        except Exception as e:
            self._chat_write(Text(f"Switch failed: {e}", style="bold red"))
            self._set_activity(Text("Error", style="red"))
            input_w.disabled = False
            input_w.focus()
            return

        # Same workflow, just a different backend for subsequent turns
        self._current_backend = backend.type
        self._chat_write(Text(f"Switched to {backend.type}!", style="green"))
        self._set_liveness(Text(f"● Active [{self._current_backend}]", style="green"))
        self._set_activity()
        input_w.disabled = False
        input_w.focus()

    @work
    async def _send_message(self, message: str) -> None:
        """Signal the workflow with the user message then poll get_turn_state
        until the turn is complete or needs approval.  No concurrent timers —
        this single worker owns the entire interaction loop."""
        input_w = self.query_one("#chat-input", Input)
        assert self._handle is not None

        # Signal is fire-and-forget — returns immediately
        try:
            await self._handle.signal(self._workflow_cls.send_message, message)
        except Exception as e:
            self._chat_write(Text(f"Error sending message: {e}", style="bold red"))
            self._set_activity(Text("Error — try again", style="red"))
            input_w.disabled = False
            input_w.focus()
            return

        # Poll until the workflow has started and finished this turn.
        # We track turn_id so we don't mistake a stale "complete" from a
        # previous turn as the response to this message.
        while True:
            await asyncio.sleep(1)
            try:
                state: TurnState = await self._handle.query(self._workflow_cls.get_turn_state)
            except Exception as e:
                self._set_activity(Text(f"Poll error: {e}", style="red"))
                continue

            # Render tool calls as they appear / update
            if state.tool_calls:
                await self._render_live_tool_calls(state)

            # Wait until the workflow has actually started a new turn
            if state.turn_id <= self._current_turn_id:
                self._set_activity(Text("Waiting...", style="dim"))
                continue

            if state.status == "thinking":
                self._set_activity(Text("Thinking...", style="cyan"))

            elif state.status == "awaiting_approval":
                # Don't update _current_turn_id here — the approval
                # continuation is the same turn, so the turn_id check
                # must still pass when we resume polling after "yes"/"no".
                tool_desc = state.approval_request.description if state.approval_request else ""
                self._chat_write(Text(f"\n[approval needed] {tool_desc}", style="yellow"))
                self._set_activity(Text("Approval required", style="yellow"))
                self.query_one("#approval-label", Static).update(Text(tool_desc))
                input_w.display = False
                self.query_one("#approval-bar").display = True
                break

            elif state.status == "complete":
                self._current_turn_id = state.turn_id
                if state.response_text:
                    self._chat_write(Markdown(state.response_text))
                self._set_activity()
                input_w.disabled = False
                input_w.focus()
                break

    # -- Approval flow ------------------------------------------------------

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        btn = event.button.id

        # Backend picker buttons
        if btn == "btn-backend-daytona":
            self.query_one("#backend-picker").display = False
            self._on_backend_chosen(DaytonaBackendConfig())
            return
        if btn == "btn-backend-docker":
            self.query_one("#backend-picker").display = False
            self._on_backend_chosen(DockerBackendConfig())
            return
        if btn == "btn-backend-e2b":
            self.query_one("#backend-picker").display = False
            self._on_backend_chosen(E2BBackendConfig())
            return
        if btn == "btn-backend-local":
            self.query_one("#backend-picker").display = False
            # Show workspace root picker with default = cwd/workspace
            default_root = str(Path(self._cwd) / "workspace")
            ws_input = self.query_one("#workspace-input", Input)
            ws_input.value = default_root
            self.query_one("#workspace-picker").display = True
            ws_input.focus()
            self._set_liveness("Choose workspace root")
            return

        # Workspace picker buttons
        if btn == "btn-workspace-accept":
            self.query_one("#workspace-picker").display = False
            raw = self.query_one("#workspace-input", Input).value.strip()
            workspace_root = Path(raw) if raw else Path(self._cwd) / "workspace"
            self._on_backend_chosen(LocalBackendConfig(workspace_root=workspace_root))
            return
        if btn == "btn-workspace-cancel":
            self.query_one("#workspace-picker").display = False
            self._show_backend_picker()
            return

        # Approval buttons
        if btn in ("btn-approve", "btn-deny"):
            approved = btn == "btn-approve"
            self._chat_write(
                Text(
                    f"  -> {'approved' if approved else 'denied'}",
                    style="green" if approved else "red",
                )
            )
            self.query_one("#approval-bar").display = False
            self.query_one("#chat-input", Input).display = True
            self.query_one("#chat-input", Input).disabled = True
            self._set_activity(Text("Thinking...", style="cyan"))
            self._send_message("yes" if approved else "no")
            return

        # Fork buttons (kept for UI compatibility, both trigger the same fork)
        if btn in ("btn-fork-copy", "btn-fork-share"):
            self.query_one("#fork-bar").display = False
            self.query_one("#chat-input", Input).display = True
            self._fork_session(self._pending_fork_title)
            self._pending_fork_title = None
            return

        # Exit buttons
        if btn == "btn-keep":
            self._on_exit_choice(keep_alive=True)
            return
        if btn == "btn-destroy":
            self._on_exit_choice(keep_alive=False)
            return

    # -- Phase 3: Exit prompt -----------------------------------------------

    def _show_exit_prompt(self) -> None:
        """Show the keep-alive / destroy choice."""
        self.query_one("#chat-input", Input).display = False
        self.query_one("#exit-bar").display = True
        self._set_activity("Choose an exit option")

    @work
    async def _on_exit_choice(self, keep_alive: bool) -> None:
        self.query_one("#exit-bar").display = False

        if keep_alive:
            # Pause the workflow so the sandbox state is persisted.
            if self._handle is not None:
                self._set_activity(Text("Saving session...", style="cyan"))
                try:
                    await self._handle.execute_update(self._workflow_cls.pause)
                except Exception:
                    pass
        else:
            assert self._manager_handle is not None
            assert self._current_workflow_id is not None
            try:
                await self._manager_handle.execute_update(
                    SessionManagerWorkflow.destroy_session,
                    self._current_workflow_id,
                )
            except Exception:
                pass

        self._return_to_session_picker()

    def _return_to_session_picker(self) -> None:
        """Reset chat state and show the session picker again."""
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None
        self._handle = None
        self._current_workflow_id = None

        # Hide chat UI
        self._chat_clear()
        self.query_one("#chat").display = False
        self.query_one("#chat-input", Input).display = False
        self.query_one("#approval-bar").display = False
        self.query_one("#fork-bar").display = False
        self.query_one("#exit-bar").display = False
        self.query_one("#snapshot-picker").display = False
        self.query_one("#backend-picker").display = False
        self.query_one("#workspace-picker").display = False

        # Re-populate and show the session picker
        self.sub_title = "Temporal Workflow"
        self._refresh_session_picker()

    @work
    async def _refresh_session_picker(self) -> None:
        """Re-query sessions and show the picker tree."""
        assert self._manager_handle is not None
        tree = self.query_one("#session-picker", Tree)
        sessions = await self._manager_handle.query(SessionManagerWorkflow.list_sessions)
        await self._backfill_snapshot_ids(sessions)
        self._populate_session_tree(tree, sessions)
        tree.root.expand_all()
        tree.display = True
        self._set_liveness("Select a session")
        self._set_activity()
        tree.focus()

    # -- Graceful quit (Ctrl+C) ---------------------------------------------

    def action_quit_graceful(self) -> None:
        if self._handle:
            # In a session — show the keep-alive / destroy prompt
            self._show_exit_prompt()
        else:
            # At the session picker — exit the TUI
            self.exit()
