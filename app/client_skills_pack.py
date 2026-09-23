"""Pack managed client skills for desktop download and admin upload."""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

from app.codex_gateway import GatewayError
from app.config import settings

BUNDLED_SKILLS_ROOT = Path(__file__).resolve().parent / "client_skills"
CLIENT_SKILLS_ROOT = BUNDLED_SKILLS_ROOT
MAX_SKILL_BYTES = 5 * 1024 * 1024
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_FRONTMATTER_NAME = re.compile(r"(?m)^name:\s*([a-z0-9][a-z0-9-]{0,63})\s*$")
_SKIP_NAMES = {".minking-managed", ".sha256", ".DS_Store"}


def data_skills_root() -> Path:
    return Path(settings.data_dir).resolve() / "client_skills"


def bundled_skills_root() -> Path:
    return BUNDLED_SKILLS_ROOT.resolve()


def _iter_skill_files(path: Path) -> list[Path]:
    files: list[Path] = []
    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue
        if item.name in _SKIP_NAMES or "__pycache__" in item.parts:
            continue
        files.append(item)
    return files


def _safe_child(root: Path, name: str) -> Path:
    dest = (root / name).resolve()
    if dest.parent != root.resolve():
        raise GatewayError(404, "Skill not found", code="skill_not_found")
    return dest


def resolve_skill_dir(name: str) -> Path:
    if not _SKILL_NAME.fullmatch(name or ""):
        raise GatewayError(404, "Skill not found", code="skill_not_found")
    uploaded = _safe_child(data_skills_root(), name)
    if (uploaded / "SKILL.md").is_file():
        return uploaded
    bundled = _safe_child(bundled_skills_root(), name)
    if (bundled / "SKILL.md").is_file():
        return bundled
    raise GatewayError(404, "Skill not found", code="skill_not_found")


def hash_skill_dir(path: Path) -> str:
    digest = hashlib.sha256()
    for file in _iter_skill_files(path):
        rel = file.relative_to(path).as_posix().encode("utf-8")
        digest.update(rel)
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _skill_entry(path: Path, *, source: str, uploaded: bool, bundled: bool) -> dict[str, Any]:
    files = [item.relative_to(path).as_posix() for item in _iter_skill_files(path)]
    return {
        "name": path.name,
        "sha256": hash_skill_dir(path),
        "files": files,
        "source": source,
        "uploaded": uploaded,
        "bundled": bundled,
    }


def list_client_skills() -> list[dict[str, Any]]:
    bundled_root = bundled_skills_root()
    data_root = data_skills_root()
    names: set[str] = set()
    if bundled_root.is_dir():
        names.update(item.name for item in bundled_root.iterdir() if item.is_dir())
    if data_root.is_dir():
        names.update(item.name for item in data_root.iterdir() if item.is_dir())
    skills: list[dict[str, Any]] = []
    for name in sorted(names):
        if not _SKILL_NAME.fullmatch(name):
            continue
        uploaded_dir = data_root / name
        bundled_dir = bundled_root / name
        uploaded = (uploaded_dir / "SKILL.md").is_file()
        bundled = (bundled_dir / "SKILL.md").is_file()
        if uploaded:
            skills.append(_skill_entry(uploaded_dir, source="uploaded", uploaded=True, bundled=bundled))
        elif bundled:
            skills.append(_skill_entry(bundled_dir, source="bundled", uploaded=False, bundled=True))
    return skills


def zip_client_skill(name: str) -> tuple[bytes, str]:
    path = resolve_skill_dir(name)
    digest = hash_skill_dir(path)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in _iter_skill_files(path):
            archive.write(file, file.relative_to(path).as_posix())
    return buffer.getvalue(), digest


def _zip_prefix(names: list[str]) -> str:
    if not names:
        return ""
    first = names[0].split("/", 1)[0]
    if not first or first == "SKILL.md":
        return ""
    prefix = first + "/"
    if all(item.startswith(prefix) or item == first for item in names):
        return prefix
    return ""


def _read_frontmatter_name(text: str) -> str:
    match = _FRONTMATTER_NAME.search(text or "")
    return match.group(1) if match else ""


def install_skill_zip(blob: bytes, *, name: str = "") -> dict[str, Any]:
    if not blob:
        raise GatewayError(400, "技能包为空", code="invalid_skill_zip")
    if len(blob) > MAX_SKILL_BYTES:
        raise GatewayError(400, "技能包过大", code="invalid_skill_zip")
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise GatewayError(400, "技能包无效", code="invalid_skill_zip") from exc
    with archive:
        members: list[tuple[zipfile.ZipInfo, str]] = []
        total = 0
        for info in archive.infolist():
            rel = info.filename.replace("\\", "/").lstrip("/")
            if not rel or rel.endswith("/"):
                continue
            path = Path(rel)
            if path.is_absolute() or ".." in path.parts:
                raise GatewayError(400, "技能包无效", code="invalid_skill_zip")
            if path.name in _SKIP_NAMES or "__pycache__" in path.parts:
                continue
            total += max(info.file_size, 0)
            if total > MAX_SKILL_BYTES:
                raise GatewayError(400, "技能包过大", code="invalid_skill_zip")
            members.append((info, rel))
        if not members:
            raise GatewayError(400, "技能包无效", code="invalid_skill_zip")
        prefix = _zip_prefix([rel for _info, rel in members])
        files: dict[str, bytes] = {}
        for info, rel in members:
            dest = rel[len(prefix) :] if prefix and rel.startswith(prefix) else rel
            if not dest or dest.endswith("/") or Path(dest).is_absolute() or ".." in Path(dest).parts:
                raise GatewayError(400, "技能包无效", code="invalid_skill_zip")
            files[dest] = archive.read(info)
    skill_md = files.get("SKILL.md")
    if skill_md is None:
        raise GatewayError(400, "技能包缺少 SKILL.md", code="invalid_skill_zip")
    declared = _read_frontmatter_name(skill_md.decode("utf-8", errors="replace"))
    folder = prefix.rstrip("/")
    chosen = (name or "").strip() or declared or (folder if _SKILL_NAME.fullmatch(folder) else "")
    if not _SKILL_NAME.fullmatch(chosen):
        raise GatewayError(400, "技能名称无效", code="invalid_skill_name")
    if declared and declared != chosen:
        raise GatewayError(400, "技能名称与 SKILL.md 不一致", code="invalid_skill_name")
    root = data_skills_root()
    root.mkdir(parents=True, exist_ok=True)
    dest = _safe_child(root, chosen)
    tmp = root / f".{chosen}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        for rel, payload in files.items():
            target = (tmp / rel).resolve()
            if tmp.resolve() not in target.parents and target != tmp.resolve():
                raise GatewayError(400, "技能包无效", code="invalid_skill_zip")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        if dest.exists():
            shutil.rmtree(dest)
        tmp.replace(dest)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return _skill_entry(
        dest,
        source="uploaded",
        uploaded=True,
        bundled=(bundled_skills_root() / chosen / "SKILL.md").is_file(),
    )


def restore_uploaded_skill(name: str) -> dict[str, Any]:
    if not _SKILL_NAME.fullmatch(name or ""):
        raise GatewayError(404, "Skill not found", code="skill_not_found")
    uploaded = _safe_child(data_skills_root(), name)
    if not uploaded.exists():
        raise GatewayError(409, "没有可恢复的上传版本", code="skill_not_uploaded")
    shutil.rmtree(uploaded, ignore_errors=True)
    bundled = bundled_skills_root() / name
    if (bundled / "SKILL.md").is_file():
        return _skill_entry(bundled, source="bundled", uploaded=False, bundled=True)
    return {"name": name, "removed": True, "source": "removed", "uploaded": False, "bundled": False}
