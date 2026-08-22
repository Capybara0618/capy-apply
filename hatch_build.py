"""Build hook that bundles the Vite WebUI into capybot/web/dist.

Source builds use the committed package-lock.json through `npm ci`. Editable
installs skip the bundle because frontend development uses the Vite dev server.
An sdist already contains a prebuilt bundle and therefore has no webui source
tree to rebuild.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class WebUIBuildHook(BuildHookInterface):
    PLUGIN_NAME = "webui-build"

    def initialize(self, version: str, build_data: dict) -> None:  # noqa: D401
        root = Path(self.root)
        webui_dir = root / "webui"
        package_json = webui_dir / "package.json"
        dist_dir = root / "capybot" / "web" / "dist"
        index_html = dist_dir / "index.html"

        if self.target_name == "wheel" and version == "editable":
            self.app.display_info(
                "[webui-build] skipped for editable install "
                "(use `cd webui && npm run build` to bundle manually)"
            )
            return

        if os.environ.get("CAPYBOT_SKIP_WEBUI_BUILD") == "1":
            self.app.display_info("[webui-build] skipped via CAPYBOT_SKIP_WEBUI_BUILD=1")
            return

        if not package_json.is_file():
            self.app.display_info(
                "[webui-build] no webui source tree, assuming prebuilt capybot/web/dist"
            )
            return

        force = os.environ.get("CAPYBOT_FORCE_WEBUI_BUILD") == "1"
        if index_html.is_file() and not force:
            self.app.display_info(
                f"[webui-build] reusing existing build at {dist_dir} "
                "(set CAPYBOT_FORCE_WEBUI_BUILD=1 to rebuild)"
            )
            return

        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeError(
                "[webui-build] npm is not available on PATH; install Node.js "
                "or set CAPYBOT_SKIP_WEBUI_BUILD=1 to bypass"
            )

        self.app.display_info("[webui-build] using npm to build webui")
        self._run([npm, "ci"], cwd=webui_dir)
        self._run([npm, "run", "build"], cwd=webui_dir)

        if not index_html.is_file():
            raise RuntimeError(
                f"[webui-build] build finished but {index_html} is missing; "
                "check webui/vite.config.ts outDir"
            )
        self.app.display_info(f"[webui-build] webui ready at {dist_dir}")

    def _run(self, cmd: list[str], *, cwd: Path) -> None:
        self.app.display_info(f"[webui-build] $ {' '.join(cmd)} (cwd={cwd})")
        try:
            subprocess.run(cmd, cwd=cwd, check=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"[webui-build] command failed ({exc.returncode}): {' '.join(cmd)}"
            ) from exc
