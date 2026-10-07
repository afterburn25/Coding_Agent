"""Project scaffolding — honest, minimal templates that actually build.

Each template produces a small but genuinely runnable project. Templates
never pretend a toolchain exists: the emitted README records the detected
commands and the verification path (build/test/dev) is reported back so
the builder loop can run it for real instead of claiming it did.

A scaffold refuses to write into a non-empty directory unless
``force=True`` — creating a project must never clobber user files.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

TEMPLATES: dict[str, dict[str, Any]] = {}


def _tpl(tid: str, name: str, description: str, files: dict[str, str],
         *, commands: dict[str, str] | None = None,
         needs: tuple[str, ...] = ()) -> None:
    TEMPLATES[tid] = {
        "id": tid, "name": name, "description": description,
        "files": files, "commands": commands or {}, "needs": needs,
    }


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(name or "")).strip("-.")
    return cleaned or "app"


# ---------------------------------------------------------------------------
# static site
# ---------------------------------------------------------------------------

_tpl("static_site", "Static website",
     "Plain HTML/CSS/JS site — no build step, preview via any web server.",
     {
         "index.html": """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{{name}}</title>
  <link rel="stylesheet" href="styles.css" />
</head>
<body>
  <main class="page">
    <h1>{{name}}</h1>
    <p id="status">Static site scaffolded by Nexus.</p>
    <button id="hello" type="button">Check it works</button>
  </main>
  <script src="app.js"></script>
</body>
</html>
""",
         "styles.css": """body { margin: 0; font-family: system-ui, sans-serif;
  background: #0f1115; color: #e8eaf0; }
.page { max-width: 720px; margin: 4rem auto; padding: 0 1.5rem; }
button { padding: .6rem 1rem; border-radius: 8px; border: 0;
  background: #4f7cff; color: #fff; cursor: pointer; }
""",
         "app.js": """document.getElementById('hello').addEventListener('click', () => {
  document.getElementById('status').textContent =
    'JS works — ' + new Date().toLocaleTimeString();
});
""",
         "README.md": "# {{name}}\n\n{{description}}\n\nOpen `index.html` or serve with `python -m http.server`.\n",
     },
     commands={"dev": "python -m http.server 8000"})


# ---------------------------------------------------------------------------
# python application / cli
# ---------------------------------------------------------------------------

_tpl("python_app", "Python application",
     "Package layout with a unittest suite — verifiable out of the box.",
     {
         "{{pkg}}/__init__.py": "\"\"\"{{name}} — scaffolded by Nexus.\"\"\"\n",
         "{{pkg}}/main.py": """\"\"\"Application entry point.\"\"\"


def greeting() -> str:
    return "hello from {{name}}"


def main() -> int:
    print(greeting())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
         "tests/__init__.py": "",
         "tests/test_main.py": """import unittest

from {{pkg}}.main import greeting


class MainTests(unittest.TestCase):
    def test_greeting(self):
        self.assertIn("hello", greeting())


if __name__ == "__main__":
    unittest.main()
""",
         "pyproject.toml": """[project]
name = "{{pkg}}"
version = "0.1.0"
description = "{{description}}"
requires-python = ">=3.10"
""",
         "README.md": "# {{name}}\n\n{{description}}\n\n```\npython -m unittest discover -s tests\npython -m {{pkg}}.main\n```\n",
     },
     commands={"test": "python -m unittest discover -s tests",
               "run": "python -m {{pkg}}.main"},
     needs=("python",))

_tpl("python_cli", "Python CLI",
     "argparse command-line tool with tests.",
     {
         "{{pkg}}/__init__.py": "",
         "{{pkg}}/cli.py": """\"\"\"{{name}} command line interface.\"\"\"
import argparse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="{{pkg}}",
                                description="{{description}}")
    p.add_argument("--echo", help="print the argument back")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.echo is not None:
        print(args.echo)
    else:
        build_parser().print_help()
    return 0
""",
         "tests/__init__.py": "",
         "tests/test_cli.py": """import unittest
from {{pkg}}.cli import build_parser, main


class CliTests(unittest.TestCase):
    def test_echo(self):
        args = build_parser().parse_args(["--echo", "hi"])
        self.assertEqual(args.echo, "hi")
        self.assertEqual(main(["--echo", "x"]), 0)


if __name__ == "__main__":
    unittest.main()
""",
         "pyproject.toml": """[project]
name = "{{pkg}}"
version = "0.1.0"
description = "{{description}}"
requires-python = ">=3.10"

[project.scripts]
{{pkg}} = "{{pkg}}.cli:main"
""",
         "README.md": "# {{name}}\n\n{{description}}\n\n```\npython -m unittest discover -s tests\npython -m {{pkg}}.cli --help\n```\n",
     },
     commands={"test": "python -m unittest discover -s tests",
               "run": "python -m {{pkg}}.cli --help"},
     needs=("python",))


# ---------------------------------------------------------------------------
# fastapi service
# ---------------------------------------------------------------------------

_tpl("fastapi_service", "FastAPI service",
     "REST API with /health, OpenAPI docs, and a TestClient suite.",
     {
         "app/__init__.py": "",
         "app/main.py": """from fastapi import FastAPI

app = FastAPI(title="{{name}}")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "{{pkg}}"}


@app.get("/")
def root() -> dict:
    return {"message": "welcome to {{name}}"}
""",
         "tests/__init__.py": "",
         "tests/test_health.py": """import unittest

from fastapi.testclient import TestClient
from app.main import app


class HealthTests(unittest.TestCase):
    def test_health(self):
        client = TestClient(app)
        r = client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])


if __name__ == "__main__":
    unittest.main()
""",
         "requirements.txt": "fastapi>=0.110\nuvicorn[standard]>=0.29\nhttpx>=0.27\n",
         "README.md": "# {{name}}\n\n{{description}}\n\n```\npip install -r requirements.txt\npython -m uvicorn app.main:app --port 8000\n```\n",
     },
     commands={"test": "python -m unittest discover -s tests",
               "dev": "python -m uvicorn app.main:app --port 8000",
               "deps": "pip install -r requirements.txt"},
     needs=("python",))


# ---------------------------------------------------------------------------
# react (vite)
# ---------------------------------------------------------------------------

_tpl("react_vite", "React (Vite)",
     "React app with a Vite build — npm install then npm run dev/build.",
     {
         "package.json": """{
  "name": "{{pkg}}",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "@vitejs/plugin-react": "^4.3.0",
    "vite": "^5.4.0"
  }
}
""",
         "vite.config.js": """import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({ plugins: [react()] })
""",
         "index.html": """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{{name}}</title>
  </head>
  <body><div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
""",
         "src/main.jsx": """import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import './styles.css'

createRoot(document.getElementById('root')).render(<App />)
""",
         "src/App.jsx": """import React, { useState } from 'react'

export default function App() {
  const [n, setN] = useState(0)
  return (
    <main className="page">
      <h1>{{name}}</h1>
      <p>React scaffolded by Nexus.</p>
      <button onClick={() => setN(n + 1)}>count: {n}</button>
    </main>
  )
}
""",
         "src/styles.css": """body { margin: 0; font-family: system-ui, sans-serif; }
.page { max-width: 720px; margin: 4rem auto; padding: 0 1.5rem; }
button { padding: .6rem 1rem; border-radius: 8px; }
""",
         "README.md": "# {{name}}\n\n{{description}}\n\n```\nnpm install\nnpm run dev\nnpm run build\n```\n",
     },
     commands={"deps": "npm install", "dev": "npm run dev",
               "build": "npm run build"},
     needs=("node", "npm"))


# ---------------------------------------------------------------------------
# node api
# ---------------------------------------------------------------------------

_tpl("node_api", "Node API (Express)",
     "Express REST service with /health and a node:test suite.",
     {
         "package.json": """{
  "name": "{{pkg}}",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "start": "node src/index.js",
    "dev": "node --watch src/index.js",
    "test": "node --test"
  },
  "dependencies": { "express": "^4.19.0" }
}
""",
         "src/index.js": """import express from 'express'

const app = express()
app.use(express.json())

app.get('/health', (req, res) => res.json({ ok: true }))
app.get('/', (req, res) => res.json({ message: 'welcome to {{name}}' }))

const port = process.env.PORT || 3000
if (process.env.NODE_ENV !== 'test') {
  app.listen(port, () => console.log(`{{name}} listening on ${port}`))
}
export default app
""",
         "test/health.test.js": """import test from 'node:test'
import assert from 'node:assert/strict'
import app from '../src/index.js'

test('health endpoint responds', async () => {
  const server = app.listen(0)
  const port = server.address().port
  const res = await fetch(`http://127.0.0.1:${port}/health`)
  assert.equal(res.status, 200)
  assert.equal((await res.json()).ok, true)
  server.close()
})
""",
         "README.md": "# {{name}}\n\n{{description}}\n\n```\nnpm install\nnpm test\nnpm start\n```\n",
     },
     commands={"deps": "npm install", "test": "npm test",
               "dev": "npm run dev"},
     needs=("node", "npm"))


# ---------------------------------------------------------------------------
# c++ cmake
# ---------------------------------------------------------------------------

_tpl("cpp_cmake", "C++ (CMake)",
     "CMake executable with a trivial test target.",
     {
         "CMakeLists.txt": """cmake_minimum_required(VERSION 3.16)
project({{pkg}} LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
add_executable({{pkg}} src/main.cpp)
enable_testing()
add_test(NAME smoke COMMAND {{pkg}} --selftest)
""",
         "src/main.cpp": """#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc > 1 && std::string(argv[1]) == "--selftest") {
        std::cout << "selftest ok\n";
        return 0;
    }
    std::cout << "hello from {{name}}\n";
    return 0;
}
""",
         "README.md": "# {{name}}\n\n{{description}}\n\n```\ncmake -S . -B build\ncmake --build build\nctest --test-dir build\n```\n",
     },
     commands={"configure": "cmake -S . -B build",
               "build": "cmake --build build",
               "test": "ctest --test-dir build --output-on-failure"},
     needs=("cmake",))


# ---------------------------------------------------------------------------
# flask service
# ---------------------------------------------------------------------------

_tpl("flask_service", "Flask service",
     "Minimal Flask app with /health and a unittest suite.",
     {
         "app/__init__.py": "",
         "app/main.py": """from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/health")
def health():
    return jsonify(ok=True, service="{{pkg}}")


@app.get("/")
def root():
    return jsonify(message="welcome to {{name}}")
""",
         "tests/__init__.py": "",
         "tests/test_health.py": """import unittest

from app.main import app


class HealthTests(unittest.TestCase):
    def test_health(self):
        client = app.test_client()
        r = client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
""",
         "requirements.txt": "flask>=3.0\n",
         "README.md": "# {{name}}\n\n{{description}}\n\n```\npip install -r requirements.txt\npython -m unittest discover -s tests\nflask --app app.main run\n```\n",
     },
     commands={"test": "python -m unittest discover -s tests",
               "dev": "flask --app app.main run --port 8000",
               "deps": "pip install -r requirements.txt"},
     needs=("python",))


# ---------------------------------------------------------------------------
# django app
# ---------------------------------------------------------------------------

_tpl("django_app", "Django app",
     "Django project with one app, a view, and the built-in test runner.",
     {
         "manage.py": """#!/usr/bin/env python
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "{{pkg}}.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
""",
         "{{pkg}}/__init__.py": "",
         "{{pkg}}/settings.py": """from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "dev-only-scaffold-key"
DEBUG = True
ALLOWED_HOSTS = ["*"]
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "web",
]
ROOT_URLCONF = "{{pkg}}.urls"
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3",
                         "NAME": BASE_DIR / "db.sqlite3"}}
USE_TZ = True
""",
         "{{pkg}}/urls.py": """from django.urls import path

from web.views import health, index

urlpatterns = [
    path("", index),
    path("health", health),
]
""",
         "web/__init__.py": "",
         "web/views.py": """from django.http import JsonResponse


def index(request):
    return JsonResponse({"message": "welcome to {{name}}"})


def health(request):
    return JsonResponse({"ok": True, "service": "{{pkg}}"})
""",
         "web/tests.py": """from django.test import Client, TestCase


class HealthTests(TestCase):
    def test_health(self):
        r = Client().get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
""",
         "requirements.txt": "django>=4.2\n",
         "README.md": "# {{name}}\n\n{{description}}\n\n```\npip install -r requirements.txt\npython manage.py test\npython manage.py runserver\n```\n",
     },
     commands={"test": "python manage.py test",
               "dev": "python manage.py runserver",
               "deps": "pip install -r requirements.txt"},
     needs=("python",))


# ---------------------------------------------------------------------------
# vue (vite)
# ---------------------------------------------------------------------------

_tpl("vue_vite", "Vue (Vite)",
     "Vue 3 app with a Vite build — npm install then npm run dev/build.",
     {
         "package.json": """{
  "name": "{{pkg}}",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "vue": "^3.4.0"
  },
  "devDependencies": {
    "@vitejs/plugin-vue": "^5.0.0",
    "vite": "^5.4.0"
  }
}
""",
         "vite.config.js": """import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({ plugins: [vue()] })
""",
         "index.html": """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{{name}}</title>
  </head>
  <body><div id="app"></div>
    <script type="module" src="/src/main.js"></script>
  </body>
</html>
""",
         "src/main.js": """import { createApp } from 'vue'
import App from './App.vue'
import './styles.css'

createApp(App).mount('#app')
""",
         "src/App.vue": """<script setup>
import { ref } from 'vue'
const n = ref(0)
</script>

<template>
  <main class="page">
    <h1>{{name}}</h1>
    <p>Vue scaffolded by Nexus.</p>
    <button @click="n++">count: {{ n }}</button>
  </main>
</template>
""",
         "src/styles.css": """body { margin: 0; font-family: system-ui, sans-serif; }
.page { max-width: 720px; margin: 4rem auto; padding: 0 1.5rem; }
button { padding: .6rem 1rem; border-radius: 8px; }
""",
         "README.md": "# {{name}}\n\n{{description}}\n\n```\nnpm install\nnpm run dev\nnpm run build\n```\n",
     },
     commands={"deps": "npm install", "dev": "npm run dev",
               "build": "npm run build"},
     needs=("node", "npm"))


# ---------------------------------------------------------------------------
# .NET console + web api
# ---------------------------------------------------------------------------

_tpl("dotnet_console", ".NET console",
     "C# console project — builds and runs with the dotnet SDK.",
     {
         "{{pkg}}.csproj": """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>enable</Nullable>
  </PropertyGroup>
</Project>
""",
         "Program.cs": """Console.WriteLine("hello from {{name}}");
""",
         "README.md": "# {{name}}\n\n{{description}}\n\n```\ndotnet build\ndotnet run\n```\n",
     },
     commands={"build": "dotnet build", "run": "dotnet run"},
     needs=("dotnet",))

_tpl("dotnet_webapi", ".NET minimal API",
     "ASP.NET Core minimal API with /health — dotnet run to serve.",
     {
         "{{pkg}}.csproj": """<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>enable</Nullable>
  </PropertyGroup>
</Project>
""",
         "Program.cs": """var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();

app.MapGet("/health", () => Results.Json(new { ok = true }));
app.MapGet("/", () => Results.Json(new { message = "welcome to {{name}}" }));

app.Run();
""",
         "README.md": "# {{name}}\n\n{{description}}\n\n```\ndotnet build\ndotnet run\n```\n",
     },
     commands={"build": "dotnet build", "dev": "dotnet run"},
     needs=("dotnet",))


# ---------------------------------------------------------------------------
# go cli
# ---------------------------------------------------------------------------

_tpl("go_cli", "Go CLI",
     "Single-module Go command with a go test suite — stdlib only.",
     {
         "go.mod": "module {{pkg}}\n\ngo 1.21\n",
         "main.go": """package main

import "fmt"

func greeting() string {
	return "hello from {{name}}"
}

func main() {
	fmt.Println(greeting())
}
""",
         "main_test.go": """package main

import "testing"

func TestGreeting(t *testing.T) {
	if greeting() == "" {
		t.Fatal("empty greeting")
	}
}
""",
         "README.md": "# {{name}}\n\n{{description}}\n\n```\ngo test ./...\ngo run .\n```\n",
     },
     commands={"test": "go test ./...", "run": "go run .",
               "build": "go build -o bin/"},
     needs=("go",))


# ---------------------------------------------------------------------------
# rust cli
# ---------------------------------------------------------------------------

_tpl("rust_cli", "Rust CLI",
     "Cargo binary crate with a unit test — cargo build/test.",
     {
         "Cargo.toml": """[package]
name = "{{pkg}}"
version = "0.1.0"
edition = "2021"
""",
         "src/main.rs": """fn greeting() -> &'static str {
    "hello from {{name}}"
}

fn main() {
    println!("{}", greeting());
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn greeting_not_empty() {
        assert!(!greeting().is_empty());
    }
}
""",
         "README.md": "# {{name}}\n\n{{description}}\n\n```\ncargo test\ncargo run\n```\n",
     },
     commands={"test": "cargo test", "build": "cargo build",
               "run": "cargo run"},
     needs=("cargo",))


# ---------------------------------------------------------------------------
# python library (pip-installable)
# ---------------------------------------------------------------------------

_tpl("python_lib", "Python library",
     "Importable package with pyproject metadata and a unittest suite.",
     {
         "{{pkg}}/__init__.py": """\"\"\"{{name}} — {{description}}\"\"\"

__version__ = "0.1.0"


def about() -> str:
    return "{{name}} 0.1.0"
""",
         "tests/__init__.py": "",
         "tests/test_pkg.py": """import unittest

import {{pkg}}


class PkgTests(unittest.TestCase):
    def test_about(self):
        self.assertIn("{{pkg}}", {{pkg}}.about())


if __name__ == "__main__":
    unittest.main()
""",
         "pyproject.toml": """[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "{{pkg}}"
version = "0.1.0"
description = "{{description}}"
requires-python = ">=3.10"

[tool.setuptools.packages.find]
include = ["{{pkg}}*"]
""",
         "README.md": "# {{name}}\n\n{{description}}\n\n```\npython -m unittest discover -s tests\npip install -e .\n```\n",
     },
     commands={"test": "python -m unittest discover -s tests",
               "deps": "pip install -e ."},
     needs=("python",))


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------

def render_files(template_id: str, *, name: str,
                 description: str = "") -> dict[str, str]:
    tpl = TEMPLATES.get(template_id)
    if tpl is None:
        raise KeyError(f"unknown template '{template_id}' — "
                       f"available: {', '.join(sorted(TEMPLATES))}")
    pkg = _safe_name(name).lower().replace("-", "_").replace(".", "_")
    display = _safe_name(name)
    desc = description or tpl["description"]
    out: dict[str, str] = {}
    for rel, body in tpl["files"].items():
        rel_out = rel.replace("{{pkg}}", pkg)
        out[rel_out] = (body.replace("{{name}}", display)
                        .replace("{{pkg}}", pkg)
                        .replace("{{description}}", desc))
    return out


def scaffold(path: Path, template_id: str, *, name: str | None = None,
             description: str = "", force: bool = False) -> dict:
    """Write a template into ``path``. Refuses non-empty directories unless
    ``force`` — scaffolding must never clobber user files."""
    root = Path(path)
    if root.exists() and not root.is_dir():
        raise ValueError(f"{root} exists and is not a directory")
    if root.is_dir():
        try:
            if any(root.iterdir()) and not force:
                raise ValueError(
                    f"{root} is not empty — pass force=true to scaffold "
                    "into a non-empty directory")
        except FileNotFoundError:
            pass
    files = render_files(template_id, name=name or root.name,
                         description=description)
    written: list[str] = []
    root.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        written.append(rel)
    tpl = TEMPLATES[template_id]
    return {
        "root": str(root),
        "template": template_id,
        "files": sorted(written),
        "commands": {k: v.replace("{{pkg}}",
                                  _safe_name(name or root.name).lower()
                                  .replace("-", "_").replace(".", "_"))
                     for k, v in tpl["commands"].items()},
        "needs": list(tpl["needs"]),
    }


def catalog() -> list[dict[str, str]]:
    return [{"id": t["id"], "name": t["name"],
             "description": t["description"],
             "needs": list(t["needs"])} for t in TEMPLATES.values()]
