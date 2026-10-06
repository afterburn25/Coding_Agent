"""Deterministic slash-command system.

A leading ``/name [args]`` message parses to a typed command, resolves
through :class:`CommandRegistry`, and executes via :class:`CommandExecutor`
— straight into existing Nexus services/ActionRegistry actions. Commands
never reach a model.
"""
from .types import CommandResult, CommandSpec, ParsedCommand
from .parser import parse_command
from .registry import CommandRegistry
from .executor import CommandExecutor
from .core import THINK_MODES, register_core_commands

__all__ = [
    "CommandResult", "CommandSpec", "ParsedCommand",
    "parse_command", "CommandRegistry", "CommandExecutor",
    "THINK_MODES", "register_core_commands",
]
