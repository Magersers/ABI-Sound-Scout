"""Download the public ABI Builder merchant reference page and public audio.

No login, private API, companion service, or game process access is used.
Only Python's standard library is required. This is a local reference cache;
the third-party game data and sounds are not covered by our source-code license.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import warnings
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

SOURCE_URL = "https://abibuilder.com/merchant"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data"


def download(url: str, max_bytes: int = 12_000_000) -> bytes:
    last_error = None
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": "ABI-Sound-Scout/0.1 (personal reference cache)"})
            with urlopen(req, timeout=30) as response:
                payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise ValueError(f"Response exceeds {max_bytes} bytes: {url}")
            return payload
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    raise RuntimeError(f"Could not download {url}: {last_error}") from last_error


def extract_catalog(html: str) -> dict:
    # Next.js emits public page props as JSON-encoded RSC strings. Decode strings,
    # then parse the data object with JSONDecoder instead of evaluating JavaScript.
    chunks = []
    for match in re.finditer(r"self\.__next_f\.push\((\[.*?\])\)</script>", html, re.S):
        try:
            chunk = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        if len(chunk) == 2 and chunk[0] == 1 and isinstance(chunk[1], str):
            chunks.append(chunk[1])
    payload = "".join(chunks)
    decoder = json.JSONDecoder()
    for match in re.finditer(r'"data":\s*(\{)', payload):
        try:
            data, _ = decoder.raw_decode(payload[match.start(1):])
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("items"), list) and isinstance(data.get("sounds"), list):
            if data["items"] and data["sounds"]:
                return data
    raise ValueError("The public page structure changed; no complete merchant catalog was found.")


def validate_catalog(data: dict) -> None:
    if not 1 <= len(data["sounds"]) <= 200:
        raise ValueError("Unexpected sound group count")
    group_ids = {int(sound["id"]) for sound in data["sounds"]}
    if len(group_ids) != len(data["sounds"]):
        raise ValueError("Duplicate sound group IDs")
    ids = set()
    for item in data["items"]:
        if item["id"] in ids:
            raise ValueError("Duplicate item ID")
        ids.add(item["id"])
        if int(item["soundId"]) not in group_ids:
            raise ValueError("Item has an unknown sound group")
        if len(item["size"]) != 2 or any(not isinstance(n, int) or n < 1 for n in item["size"]):
            raise ValueError("Item has invalid dimensions")
        if not item.get("name") or item["contactPrice"] < 0:
            raise ValueError("Item name or contact price is invalid")
    for sound in data["sounds"]:
        for action in ("pickup", "drop"):
            if not re.fullmatch(r"[A-Za-z0-9_]+", sound[action]):
                raise ValueError("Unsafe audio basename in source catalog")


def load_russian_names(path: Path) -> dict:
    """Read the local ID/name dictionary; a damaged dictionary must not erase
    usable translations from the previous catalog or prevent a data refresh.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if (not isinstance(data, dict) or data.get("schema_version") != 1
                or data.get("language") != "ru" or not isinstance(data.get("items"), dict)):
            raise ValueError("expected schema_version 1, language ru and an items dictionary")
    except (OSError, UnicodeError, ValueError) as exc:
        warnings.warn(f"Russian name dictionary unavailable: {path.name}: {exc}", RuntimeWarning)
        return {}
    valid = {}
    skipped = 0
    for identifier, entry in data["items"].items():
        if (not isinstance(identifier, str) or not identifier.isdecimal()
                or not isinstance(entry, dict)
                or not isinstance(entry.get("name_en"), str) or not entry["name_en"].strip()
                or not isinstance(entry.get("name_ru"), str) or not entry["name_ru"].strip()):
            skipped += 1
            continue
        valid[identifier] = {"name_en": entry["name_en"], "name_ru": entry["name_ru"].strip()}
    if skipped:
        warnings.warn(f"Ignored {skipped} malformed Russian name entries in {path.name}", RuntimeWarning)
    return valid


def preserve_russian_names(items: list[dict], dictionary_path: Path, previous: dict | None = None) -> int:
    """Prefer the local dictionary, then a previous translation with the exact
    same item ID and English name. Never carry a translation across an ID reuse
    or rename; new/renamed items remain in English until the dictionary catches up.
    """
    dictionary = load_russian_names(dictionary_path)
    previous_items = previous.get("items", []) if isinstance(previous, dict) else []
    previous_by_id = {str(item["id"]): item for item in previous_items
                      if isinstance(item, dict) and "id" in item} if isinstance(previous_items, list) else {}
    translated = 0
    for item in items:
        identifier = str(item["id"])
        name = item["name"]
        entry = dictionary.get(identifier)
        russian = entry["name_ru"] if entry and entry["name_en"] == name else None
        if not russian:
            old = previous_by_id.get(identifier, {})
            old_name = old.get("name_ru")
            if old.get("name") == name and isinstance(old_name, str) and old_name.strip():
                russian = old_name.strip()
        item.pop("name_ru", None)
        if russian:
            item["name_ru"] = russian
            translated += 1
    return translated


def _version_parts(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value):
        raise ValueError("expected a numeric dotted game data version")
    parts = [int(part) for part in value.split(".")]
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _sound_pair(sound: dict) -> tuple[str | None, str | None]:
    return sound.get("pickup_event"), sound.get("drop_event")


def _validate_override_sound(sound: dict) -> None:
    if (not isinstance(sound, dict) or type(sound.get("group_id")) is not int
            or sound["group_id"] < 1):
        raise ValueError("invalid override sound group ID")
    prefixes = {"abibuilder.com": "/audio/merchant/",
                "cdn-abitracker.pages.dev": "/data/merchant/audio/"}
    for action in ("pickup", "drop"):
        if sound.get(action) != f"sounds/{sound['group_id']}_{action}.ogg":
            raise ValueError("override sound path must match its group ID and action")
        event = sound.get(action + "_event")
        if not isinstance(event, str) or not re.fullmatch(r"[A-Za-z0-9_]+", event):
            raise ValueError("invalid override sound event")
        url = sound.get(action + "_url")
        if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in url):
            raise ValueError("invalid override audio URL")
        parsed = urlsplit(url)
        prefix = prefixes.get(parsed.netloc)
        if (parsed.scheme != "https" or not prefix or parsed.fragment
                or not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]+\.ogg", parsed.path)):
            raise ValueError("override audio URL must use an approved public audio location")
        if not re.fullmatch(r"[0-9a-f]{64}", str(sound.get(action + "_sha256", ""))):
            raise ValueError("override audio requires a verified SHA256 hash")


def apply_local_sound_overrides(catalog: dict, path: Path) -> int:
    """Apply version-bounded, exact item/event corrections from local evidence.

    Only sound pairs needed by matching items are imported. Prices, dimensions,
    names, and the upstream data version retain their original provenance.
    """
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if (not isinstance(data, dict) or data.get("schema_version") != 1
                or not isinstance(data.get("items"), dict)
                or not isinstance(data.get("sounds"), list)):
            raise ValueError("expected schema_version 1, items dictionary and sounds list")
        local_version = _version_parts(data.get("game_data_version"))
        upstream_version = _version_parts(catalog.get("data_version"))
        if upstream_version > local_version:
            raise ValueError("upstream data is newer than the verified local game data")
        definitions = {}
        for sound in data["sounds"]:
            _validate_override_sound(sound)
            pair = _sound_pair(sound)
            if pair in definitions:
                raise ValueError("duplicate override event pair")
            definitions[pair] = sound
        for identifier, entry in data["items"].items():
            if (not re.fullmatch(r"[0-9]+", identifier) or not isinstance(entry, dict)
                    or not isinstance(entry.get("name_en"), str) or not entry["name_en"].strip()):
                raise ValueError("invalid override item ID or English name")
            for key in ("expected_pickup_event", "expected_drop_event", "pickup_event", "drop_event"):
                if not isinstance(entry.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_]+", entry[key]):
                    raise ValueError("invalid override item event")
            if _sound_pair(entry) not in definitions:
                raise ValueError("override item has no verified target sound definition")
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        warnings.warn(f"Local sound overrides skipped: {path.name}: {exc}", RuntimeWarning)
        return 0

    by_id = {int(sound["group_id"]): sound for sound in catalog["sounds"]}
    by_pair = {_sound_pair(sound): sound for sound in catalog["sounds"]}
    used_paths = {sound[action] for sound in catalog["sounds"] for action in ("pickup", "drop")}
    applied = []
    for item in catalog["items"]:
        identifier = str(item["id"])
        entry = data["items"].get(identifier)
        if not entry or item["name"] != entry["name_en"]:
            continue
        current = by_id.get(int(item["sound_group"]), {})
        expected = entry["expected_pickup_event"], entry["expected_drop_event"]
        target_pair = _sound_pair(entry)
        if _sound_pair(current) == target_pair or _sound_pair(current) != expected:
            continue
        target = by_pair.get(target_pair)
        if target is None:
            target = dict(definitions[target_pair])
            group_id = target["group_id"]
            if group_id in by_id or any(target[action] in used_paths for action in ("pickup", "drop")):
                group_id = max(by_id, default=0) + 1
                while any(f"sounds/{group_id}_{action}.ogg" in used_paths for action in ("pickup", "drop")):
                    group_id += 1
            target["group_id"] = group_id
            for action in ("pickup", "drop"):
                target[action] = f"sounds/{group_id}_{action}.ogg"
                used_paths.add(target[action])
            catalog["sounds"].append(target)
            by_id[group_id] = target
            by_pair[target_pair] = target
        item["sound_group"] = target["group_id"]
        applied.append(identifier)
    if applied:
        catalog["local_sound_overrides"] = {
            "game_data_version": data["game_data_version"],
            "source_file": path.name,
            "applied_item_ids": applied,
        }
    return len(applied)


def fetch_audio_clip(entry: dict, action: str, output: Path, previous_urls: dict,
                     refresh_audio: bool = False) -> str:
    """Reuse a cached clip or fetch it, checking locally verified hashes first."""
    path, url = output / entry[action], entry[action + "_url"]
    expected_hash = entry.get(action + "_sha256")
    reuse = path.exists() and not refresh_audio and previous_urls.get(entry[action]) == url
    audio = path.read_bytes() if reuse else None
    if reuse and expected_hash and hashlib.sha256(audio).hexdigest() != expected_hash:
        reuse = False
    if not reuse:
        audio = download(url, 2_000_000)
    if not audio.startswith(b"OggS"):
        raise ValueError(f"Expected OGG audio: {url}")
    actual_hash = hashlib.sha256(audio).hexdigest()
    if expected_hash and actual_hash != expected_hash:
        raise ValueError(f"Audio does not match verified SHA256: {url}")
    if not reuse:
        temporary = path.with_suffix(".ogg.tmp")
        temporary.write_bytes(audio)
        temporary.replace(path)
    entry[action + "_sha256"] = actual_hash
    entry[action + "_bytes"] = len(audio)
    return path.name


def apply_local_audio_references(catalog: dict, path: Path, base: Path) -> int:
    """Attach verified local FLAC variants to exact, version-bounded events.

    Public primary clips and their URLs remain the fallback. A missing or
    damaged optional local reference never prevents a public catalog refresh.
    """
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if (not isinstance(data, dict) or data.get("schema_version") != 1
                or not isinstance(data.get("events"), dict)):
            raise ValueError("expected schema_version 1 and an events dictionary")
        if _version_parts(catalog.get("data_version")) > _version_parts(data.get("game_data_version")):
            raise ValueError("upstream data is newer than the verified local game data")
        if data.get("verified_at") is not None and not isinstance(data["verified_at"], str):
            raise ValueError("invalid local reference verification date")
        resolved_base = base.resolve()
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        warnings.warn(f"Local audio references skipped: {path.name}: {exc}", RuntimeWarning)
        return 0

    checked = {}
    applied = 0
    for sound in catalog["sounds"]:
        for action in ("pickup", "drop"):
            event = sound.get(action + "_event")
            if event not in data["events"]:
                continue
            if event not in checked:
                checked[event] = None
                try:
                    entry = data["events"][event]
                    if (not isinstance(event, str) or not re.fullmatch(r"[A-Za-z0-9_]+", event)
                            or not isinstance(entry, dict) or type(entry.get("media_id")) is not int
                            or entry["media_id"] < 1):
                        raise ValueError("invalid event or media ID")
                    relative = entry.get("path")
                    if relative != f"sounds_local/{entry['media_id']}.flac":
                        raise ValueError("local audio path must match its media ID inside sounds_local")
                    if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
                        raise ValueError("local audio requires a SHA256 hash")
                    source = (resolved_base / relative).resolve()
                    if not source.is_relative_to(resolved_base) or not source.is_file():
                        raise ValueError("local audio file is missing or outside the data folder")
                    with source.open("rb") as stream:
                        payload = stream.read(2_000_001)
                    if len(payload) > 2_000_000 or not payload.startswith(b"fLaC"):
                        raise ValueError("expected a small FLAC reference")
                    if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
                        raise ValueError("local audio SHA256 mismatch")
                    checked[event] = relative
                except (OSError, ValueError, TypeError) as exc:
                    warnings.warn(f"Local audio reference skipped: {event}: {exc}", RuntimeWarning)
            relative = checked[event]
            if relative is None:
                continue
            key = action + "_variants"
            variants = sound.get(key, [])
            if not isinstance(variants, list) or not all(isinstance(value, str) for value in variants):
                warnings.warn(f"Local audio reference skipped: {event}: invalid variants list", RuntimeWarning)
                continue
            sound[key] = list(dict.fromkeys([*variants, relative]))
            applied += 1
    if applied:
        catalog["local_audio_references"] = {
            "game_data_version": data["game_data_version"],
            "verified_at": data.get("verified_at"),
            "events_applied_count": applied,
        }
        catalog["local_game_version"] = data["game_data_version"]
        catalog["verified_at"] = data.get("verified_at")
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--html", type=Path, help="Use a saved public page for reproducible extraction")
    parser.add_argument("--refresh-audio", action="store_true", help="Download clips again even if already cached")
    args = parser.parse_args()
    html = args.html.read_text(encoding="utf-8-sig") if args.html else download(SOURCE_URL).decode("utf-8")
    raw = extract_catalog(html)
    validate_catalog(raw)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "sounds").mkdir(exist_ok=True)
    previous_urls = {}
    previous = {}
    previous_path = args.output / "catalog.json"
    if previous_path.exists():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            if not isinstance(previous, dict):
                previous = {}
            for sound in previous.get("sounds", []):
                for action in ("pickup", "drop"):
                    previous_urls[sound[action]] = sound.get(action + "_url")
        except (OSError, ValueError, KeyError, TypeError):
            pass
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    catalog = {
        "schema_version": 1,
        "fetched_at": fetched_at,
        "source_url": SOURCE_URL,
        "upstream_source_url": raw.get("meta", {}).get("source"),
        "data_version": raw.get("meta", {}).get("dataVersion", raw.get("version")),
        "source_updated_at": raw.get("meta", {}).get("scrapedAt"),
        "currency": "Koen",
        "price_semantics": {
            "contact_price": "Published contact sale price, not a live player-market quote; condition may change the sale value.",
            "reference_price": "Published reference value, not a live player-market quote.",
        },
        "license_notice": "Game assets belong to Tencent Games / Morefun Studios. ABI Builder credits Maoyan / ABI Tracker. No open redistribution license for this data/audio was found; cached locally for this user's reference.",
        "items": [],
        "sounds": [],
    }
    for item in raw["items"]:
        catalog["items"].append({
            "id": item["id"], "name": item["name"],
            "width": item["size"][0], "height": item["size"][1],
            "rarity": item["rarity"], "sound_group": item["soundId"],
            "contact_price": item["contactPrice"], "reference_price": item.get("referencePrice"),
            "category": item.get("categoryName"),
        })
    translated = preserve_russian_names(catalog["items"], args.output / "item_names_ru.json", previous)
    for sound in raw["sounds"]:
        entry = {"group_id": sound["id"]}
        for action in ("pickup", "drop"):
            relative = f"sounds/{sound['id']}_{action}.ogg"
            url = f"https://abibuilder.com/audio/merchant/{sound[action]}.ogg"
            entry[action] = relative
            entry[action + "_url"] = url
            entry[action + "_event"] = sound[action]
        catalog["sounds"].append(entry)
    corrected = apply_local_sound_overrides(catalog, args.output / "local_catalog_overrides.json")
    local_audio = apply_local_audio_references(catalog, args.output / "local_audio_references.json", args.output)
    jobs = [(entry, action) for entry in catalog["sounds"] for action in ("pickup", "drop")]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch_audio_clip, entry, action, args.output, previous_urls, args.refresh_audio)
                   for entry, action in jobs]
        for index, future in enumerate(as_completed(futures), 1):
            name = future.result()
            print(f"{index:2}/{len(jobs)} {name}", flush=True)
    target = args.output / "catalog.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    print(f"Saved {len(catalog['items'])} items, {len(catalog['sounds'])} groups to {target}")
    print(f"Preserved Russian names for {translated}/{len(catalog['items'])} items")
    print(f"Applied verified local sound corrections for {corrected} items")
    print(f"Attached verified local FLAC references to {local_audio} sound events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
