"""The runtime tools come from a lockfile, with scripts ignored, in both images."""

from pathlib import Path

from scripts.check_pi_lock import lock_problems

ROOT = Path(__file__).resolve().parent.parent
IMAGES = (ROOT / "deploy" / "Dockerfile", ROOT / "deploy" / "Dockerfile.agent")


def test_the_lock_resolves_the_manifests_pins_with_a_hash_for_everything() -> None:
    assert lock_problems() == []


def test_both_images_install_from_the_lock_with_scripts_ignored() -> None:
    for dockerfile in IMAGES:
        text = dockerfile.read_text(encoding="utf-8")
        assert "COPY deploy/pi/package.json deploy/pi/package-lock.json" in text, dockerfile
        assert "npm ci --ignore-scripts" in text, dockerfile
        assert "npm install -g" not in text, dockerfile
        assert "ARG PI_VERSION" not in text, dockerfile


def test_telemetry_is_answered_in_the_images() -> None:
    for dockerfile in IMAGES:
        assert "ENV PI_TELEMETRY=0" in dockerfile.read_text(encoding="utf-8"), dockerfile


def test_every_base_image_is_pinned_by_digest() -> None:
    """A tag moves; a digest does not. Every FROM and every COPY --from of a
    registry image names one — the browser image already did."""
    for dockerfile in (*IMAGES, ROOT / "deploy" / "Dockerfile.ward", ROOT / "deploy" / "Dockerfile.browser"):
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            if line.startswith("FROM ") or "COPY --from=" in line:
                image = line.split("--from=")[1].split()[0] if "--from=" in line else line.split()[1]
                if "/" in image and ":" in image:  # a registry image, not a build stage
                    assert "@sha256:" in image, f"{dockerfile.name}: {image} is not pinned by digest"


def test_the_checker_names_a_lock_that_lags_the_manifest_and_a_hashless_entry(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    from scripts import check_pi_lock

    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"a": "2.0.0", "b": "1.0.0"}}))
    (tmp_path / "package-lock.json").write_text(json.dumps({"packages": {
        "": {},
        "node_modules/a": {"version": "1.0.0", "integrity": "sha512-x"},
        "node_modules/b": {"version": "1.0.0"},
    }}))
    monkeypatch.setattr(check_pi_lock, "PI_DIR", tmp_path)
    found = check_pi_lock.lock_problems()
    assert any("a: package.json pins 2.0.0, the lock has 1.0.0" in line for line in found)
    assert any("node_modules/b has no integrity hash" in line for line in found)
    assert len(found) == 2


def test_filling_takes_the_missing_hashes_from_the_registry_and_nothing_else(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    from scripts import check_pi_lock

    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"a": "1.0.0"}}))
    (tmp_path / "package-lock.json").write_text(json.dumps({"packages": {
        "": {},
        "node_modules/a": {"version": "1.0.0", "integrity": "sha512-kept"},
        "node_modules/a/node_modules/b": {"version": "2.0.0"},
    }}))
    monkeypatch.setattr(check_pi_lock, "PI_DIR", tmp_path)
    asked: list[str] = []

    def fetch(spec: str) -> str:
        asked.append(spec)
        return "sha512-fromregistry"

    assert check_pi_lock.fill_integrity(fetch) == ["b@2.0.0"]
    assert asked == ["b@2.0.0"]
    lock = json.loads((tmp_path / "package-lock.json").read_text())
    assert lock["packages"]["node_modules/a/node_modules/b"]["integrity"] == "sha512-fromregistry"
    assert lock["packages"]["node_modules/a"]["integrity"] == "sha512-kept"
    assert check_pi_lock.lock_problems() == []


def test_the_registry_lookup_needs_no_npm(monkeypatch) -> None:
    """The Makefile falls back to a container when the host has no npm; the
    fill that follows must not need it either."""
    import io
    import json

    from scripts import check_pi_lock

    asked: list[str] = []

    def urlopen(url: str, timeout: float):
        asked.append(url)
        return io.BytesIO(json.dumps({"dist": {"integrity": "sha512-x"}}).encode())

    monkeypatch.setattr(check_pi_lock.urllib.request, "urlopen", urlopen)
    assert check_pi_lock._registry_integrity("@earendil-works/pi-ai@0.85.1") == "sha512-x"
    assert asked == ["https://registry.npmjs.org/@earendil-works/pi-ai/0.85.1"]


def test_fd_comes_from_its_release_checked_not_from_debian() -> None:
    """Debian bookworm's fd 8.6 lacks a flag pi 0.85 passes, which broke pi's
    `find`; the images install a pinned release and verify its checksum."""
    for dockerfile in IMAGES:
        text = dockerfile.read_text(encoding="utf-8")
        assert "fd-find" not in text, dockerfile
        assert "ARG FD_VERSION=" in text and "sha256sum -c -" in text, dockerfile
