"""Run the shipped dashboard's dependency-free JS behavior tests."""
import subprocess
from pathlib import Path


def test_kanban_deep_link_drawer():
    subprocess.run(
        ["node", "--test", str(Path(__file__).with_name("kanban_deep_link.test.cjs"))],
        check=True,
        timeout=30,
    )
