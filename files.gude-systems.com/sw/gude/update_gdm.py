#!/usr/bin/env python3
"""Generate GUDE Device Manager release metadata from GitHub releases."""

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


DEFAULT_REPO = "gudesystems/gude-device-manager"
DEFAULT_TITLE = "GUDE Device Manager"
USER_AGENT = "gdm-release-feed-generator/1.0"


def fetch_releases(repo):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def version_from_release(release):
    tag = release.get("tag_name") or release.get("name") or ""
    match = re.search(r"(\d+\.\d+\.\d+(?:[-.][A-Za-z0-9]+)*)", tag)
    return match.group(1) if match else tag.lstrip("v")


def format_date(value):
    if not value:
        return ""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.strftime("%d.%m.%Y")


def format_size(size):
    if not size:
        return ""
    mib = size / (1024 * 1024)
    if mib >= 1:
        return f"{mib:.1f} MB"
    return f"{size / 1024:.1f} KB"


def pick_asset(release):
    assets = release.get("assets") or []
    exe_assets = [asset for asset in assets if asset.get("name", "").lower().endswith(".exe")]
    if exe_assets:
        return max(exe_assets, key=lambda item: item.get("size") or 0)
    if assets:
        return max(assets, key=lambda item: item.get("size") or 0)
    return {}


def extract_section_bullets(markdown, heading):
    lines = markdown.splitlines()
    bullets = []
    in_section = False
    heading_re = re.compile(rf"^###\s+{re.escape(heading)}\s*$", re.IGNORECASE)

    for line in lines:
        stripped = line.strip()
        if heading_re.match(stripped):
            in_section = True
            continue
        if in_section and stripped.startswith("### "):
            break
        if not in_section:
            continue
        match = re.match(r"^[-*]\s+(.+?)\s*$", stripped)
        if match:
            bullets.append(clean_markdown(match.group(1)))
    return bullets


def clean_markdown(value):
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    return value.strip()


def split_summary(summary):
    fixes = []
    features = []
    for item in summary:
        lower = item.lower()
        if lower.startswith(("fix:", "fixed ", "fixes ")) or " bug" in lower:
            fixes.append(item)
        else:
            features.append(item)
    return features, fixes


def release_to_entry(release):
    asset = pick_asset(release)
    summary = extract_section_bullets(release.get("body") or "", "Summary")
    if not summary:
        summary = extract_section_bullets(release.get("body") or "", "Highlights")
    features, fixes = split_summary(summary)

    return {
        "version": version_from_release(release),
        "date": format_date(release.get("published_at") or release.get("created_at")),
        "size": format_size(asset.get("size")),
        "features": features,
        "fixes": fixes,
    }


def render_html(entries, title):
    lines = [
        "<!DOCTYPE html>",
        "<html>",
        "  <head>",
        f"    <title>{html.escape(title)} - Revision History</title>",
        "    <style>",
        "      body {",
        "        font-family: Arial, Helvetica, sans-serif;",
        "        background-color: white;",
        "      }",
        "    </style>",
        "  </head>",
        "  <body>",
        f"    <h1>{html.escape(title)}</h1>",
        "    <h2>Software Release Notes / Revision History</h2>",
        "",
    ]

    for entry in entries:
        filename = f"gdm_v{entry['version']}.zip"
        version_html = (
            f'<ul><li><a href="{html.escape(filename)}">'
            f'v{html.escape(entry["version"])} - {html.escape(entry["date"])}</a></li>'
        )
        if entry.get("features"):
            version_html += "<ul><li><b>Features</b></li><ul>"
            for item in entry["features"]:
                version_html += f"<li>{html.escape(item)}</li>"
            version_html += "</ul></ul>"
        if entry.get("fixes"):
            version_html += "<ul><li><b>Bugfixes</b></li><ul>"
            for item in entry["fixes"]:
                version_html += f"<li>{html.escape(item)}</li>"
            version_html += "</ul></ul>"
        lines.append(version_html + "</ul>")

    lines.extend(
        [
            '<hr size="1" noshade />',
            '<img src="https://www.gude.info/fileadmin/templates/img/logo_gude.png">',
            "<br />",
            '<a href="https://www.gude.info">GUDE Systems GmbH</a>',
            "</body>",
            "</html>",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--basename", default="gdm")
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--include-prereleases", action="store_true")
    args = parser.parse_args(argv)

    releases = fetch_releases(args.repo)
    entries = [
        release_to_entry(release)
        for release in releases
        if not release.get("draft") and (args.include_prereleases or not release.get("prerelease"))
    ]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"{args.basename}.json"
    html_path = args.output_dir / f"{args.basename}.html"
    json_path.write_text(json.dumps(entries) + "\n", encoding="utf-8")
    html_path.write_text(render_html(entries, args.title), encoding="utf-8")
    print(f"wrote {json_path}")
    print(f"wrote {html_path}")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as exc:
        print(f"failed to fetch GitHub releases: {exc}", file=sys.stderr)
        sys.exit(1)
