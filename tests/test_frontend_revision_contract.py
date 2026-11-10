from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendRevisionContractTests(unittest.TestCase):
    def test_frontend_image_binds_revision_at_build_time(self) -> None:
        dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        legacy_compose = (ROOT / "deploy" / "synology" / "compose.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("ARG RESOURCEPLANNER_GIT_REVISION=unavailable", dockerfile)
        self.assertIn("RESOURCEPLANNER_GIT_REVISION", compose)
        self.assertIn("RESOURCEPLANNER_GIT_REVISION", legacy_compose)
        self.assertIn("public/build-meta.js", dockerfile)
        self.assertIn("npm run build", dockerfile)
        self.assertLess(
            dockerfile.index("public/build-meta.js"),
            dockerfile.index("npm run build"),
        )
        self.assertIn("org.opencontainers.image.revision", dockerfile)
        self.assertRegex(dockerfile, r"\[0-9a-fA-F\]\{40\}")

    def test_build_helper_derives_revision_from_clean_checkout(self) -> None:
        helper = (ROOT / "tools" / "build_resourceplanner_images.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("git rev-parse --verify HEAD", helper)
        self.assertIn("git status --porcelain --untracked-files=normal", helper)
        self.assertIn("Refusing to build revision metadata from a dirty checkout", helper)
        self.assertIn("export RESOURCEPLANNER_GIT_REVISION", helper)
        self.assertIn('exec docker compose build "$@"', helper)

    def test_shell_reads_only_immutable_build_metadata_with_visible_fallback(self) -> None:
        index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        default_meta = (ROOT / "frontend" / "public" / "build-meta.js").read_text(
            encoding="utf-8"
        )
        runtime_startup = (
            ROOT / "frontend" / "20-resourceplanner-runtime-config.sh"
        ).read_text(encoding="utf-8")
        app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn('<script src="/build-meta.js"></script>', index)
        self.assertLess(
            index.index('<script src="/build-meta.js"></script>'),
            index.index('<script src="/runtime-config.js"></script>'),
        )
        self.assertIn('revision: "unavailable"', default_meta)
        self.assertNotIn("build-meta.js", runtime_startup)
        self.assertIn("window.RESOURCEPLANNER_BUILD?.revision", app)
        self.assertIn("^[0-9a-f]{40}$", app)
        self.assertIn("Révision déployée : indisponible", app)
        self.assertIn("title={DEPLOYED_GIT_REVISION_LABEL}", app)
        self.assertNotIn("github.com", app.casefold())


if __name__ == "__main__":
    unittest.main()
