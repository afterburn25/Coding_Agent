from __future__ import annotations

import base64
import json
import mimetypes
import os
import queue
import re
import secrets
import sys
import threading
import time
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from typing import Any, Callable

from types import SimpleNamespace

from .fsutil import atomic_write_text
from .procutil import no_window_flags
from . import netdiag
from .policies import EGRESS_MODES, RESOURCE_MODES, ResourcePolicies
from .release import SECTIONS as SECTIONS_RC
from .temp_specialists import DEFAULT_TTL as DEFAULT_SPECIALIST_TTL
from .agent.orchestrator import (
    AgentOrchestrator,
    UNVERIFIED_CLAIMS_MARKER as _UNVERIFIED_CLAIMS_MARKER,
)
from .image.manager import ImageManager
from .config import AgentConfig, ModelProfile, load_config
from .models.router import ModelRouter, ModelUnavailableError
from .models.telemetry import ModelPerformanceTelemetry
from .runtime.manager import RuntimeManager
from .runtime.setup import apply_detected_models, suggest_model_profiles, write_suggested_models