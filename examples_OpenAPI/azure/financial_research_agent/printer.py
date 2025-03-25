from rich.console import Console
from rich.live import Live
from rich.tree import Tree


class Printer:
    """
    Pretty-print the progress of the agent.
    """

    def __init__(self, console: Console):
        self.console = console
        self.tree = Tree("Financial Research")
        self.items: dict[str, tuple[Tree, bool]] = {}
        self.live = Live(self.tree, console=console)
        self.live.start()

    def update_item(
        self, id: str, label: str, is_done: bool = False, hide_checkmark: bool = False
    ) -> None:
        """Updates a branch of the tree, adding it if it doesn't exist."""
        if id in self.items:
            node, _ = self.items[id]
            icon = "" if hide_checkmark else ("✓ " if is_done else "⏳ ")
            node.label = f"{icon}{label}"
            self.items[id] = (node, is_done)
        else:
            icon = "" if hide_checkmark else ("✓ " if is_done else "⏳ ")
            node = self.tree.add(f"{icon}{label}")
            self.items[id] = (node, is_done)

    def mark_item_done(self, id: str) -> None:
        """Marks an item as done."""
        if id in self.items:
            node, _ = self.items[id]
            label = str(node.label)
            if label.startswith("⏳ "):
                node.label = f"✓ {label[2:]}"
            self.items[id] = (node, True)

    def end(self) -> None:
        """End the live display."""
        self.live.stop()
