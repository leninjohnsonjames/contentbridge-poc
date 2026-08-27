"""
POC: Jira ticket (title + ready-made content) -> WordPress post -> write-back

Flow:
  1. Search Jira for tickets tagged with a label (default: contentbridge-poc)
  2. For each new one: read the description, which already contains a
     "Title : ..." line followed by the full formatted article
     (headings, bullet lists, bold text, links, etc.)
  3. Extract the title and convert the rest straight to HTML - no AI
     generation needed, since the content is already written in the ticket
  4. Create that post in WordPress as a draft
  5. Comment the WordPress post link back on the Jira ticket
  6. Transition the ticket to "Review"
  7. On any failure, record the error as a Jira comment instead of crashing,
     and leave the ticket unprocessed so the next run retries it

  If a ticket doesn't follow the "Title : ..." pattern, the script falls
  back to asking Claude to draft a post from the description as a brief -
  so older-style tickets still work.

Setup:
  pip install requests

  Fill in the CONFIG block below, or set the same names as environment
  variables (recommended so you don't commit secrets to disk).
"""

import os
import json
import re
import requests

# ── CONFIG ────────────────────────────────────────────────────────────
# JIRA_AUTH_MODE:
#   "basic"  -> Jira Cloud, using your email + an API token from
#               id.atlassian.com/manage-profile/security/api-tokens
#   "bearer" -> Jira Server / Data Center, using a Personal Access Token
JIRA_AUTH_MODE   = os.getenv("JIRA_AUTH_MODE", "basic")
JIRA_API_VERSION = os.getenv("JIRA_API_VERSION", "3")   # Cloud: "3"  |  Data Center: "2"

JIRA_BASE_URL   = os.getenv("JIRA_BASE_URL", "https://bounteous.jira.com")
JIRA_EMAIL      = os.getenv("JIRA_EMAIL", "leninjohnson.james@bounteous.com")          # only used if JIRA_AUTH_MODE == "basic"
JIRA_API_TOKEN  = os.getenv("JIRA_API_TOKEN", "ATATT3xFfGF0OUbtay0ld6gXuZil8vVxJJXPzY-GV1W2tUxwlEF2kLOzYwjwz0rqRbG8v9pzn4Ju_GCbZuv-L-4H4Gcd107GgUmMbC6G5F66_NhH2OB9QIGuyTF4OaeUR3cUJ7e2y56NCr4qa3n7YOc461s6IDb4U3lHJ3QZ8MwaDMDrBvTbQkc=74D5BB2F")       # PAT (bearer) or API token (basic)
JIRA_LABEL_CREATE  = os.getenv("JIRA_LABEL_CREATE", "contentbridge-poc")         # create a new draft post
JIRA_LABEL_UPDATE  = os.getenv("JIRA_LABEL_UPDATE", "contentbridge-update-poc")  # update an existing post by title
JIRA_TARGET_STATUS = os.getenv("JIRA_TARGET_STATUS", "Code Review")   # status to move the ticket to once posted
JIRA_SOURCE_STATUS = os.getenv("JIRA_SOURCE_STATUS", "To Do")         # only pick up tickets sitting in this status

CLAUDE_API_KEY  = os.getenv("CLAUDE_API_KEY", "sk-9s_96Ibr6qqgGX6VErBrrA")
CLAUDE_MODEL    = "claude-sonnet-5"

WP_BASE_URL     = os.getenv("WP_BASE_URL", "https://dev-contentbridge.pantheonsite.io")
WP_USER         = os.getenv("WP_USER", "contentbridge-bot")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "CPbp avSv aUTr mvH3 qeGz WEzC")

PROCESSED_LOG   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "processed_issues.json")
# ─────────────────────────────────────────────────────────────────────


def _jira_get(path: str, params: dict | None = None) -> dict:
    url = f"{JIRA_BASE_URL}{path}"
    headers = {"Accept": "application/json"}

    if JIRA_AUTH_MODE == "bearer":
        headers["Authorization"] = f"Bearer {JIRA_API_TOKEN}"
        resp = requests.get(url, headers=headers, params=params, timeout=30)
    else:
        resp = requests.get(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN), headers=headers, params=params, timeout=30)

    resp.raise_for_status()
    return resp.json()


def find_issues_by_labels(labels: list[str]) -> list[dict]:
    """Uses the current Jira Cloud search endpoint (the old /rest/api/3/search
    was removed - see https://developer.atlassian.com/changelog/#CHANGE-2046).
    A bare query isn't allowed, so the label list itself doubles as the
    required search restriction.

    Also scopes the search to JIRA_SOURCE_STATUS (default "To Do"), so
    Jira's own ticket status is the source of truth for "already handled" -
    this matters once the script runs on a fresh machine every time (e.g.
    GitHub Actions), where a local processed_issues.json file can't
    persist. It also means a ticket someone manually moved to "In Progress"
    or anywhere else mid-review won't get accidentally re-touched."""
    label_list = ", ".join(f'"{label}"' for label in labels)
    jql = f'labels in ({label_list}) AND status = "{JIRA_SOURCE_STATUS}" order by created DESC'
    data = _jira_get(
        f"/rest/api/{JIRA_API_VERSION}/search/jql",
        params={"jql": jql, "maxResults": 20, "fields": "summary,description,status,labels"},
    )
    return data.get("issues", [])


def get_jira_issue(issue_key: str) -> dict:
    return _jira_get(f"/rest/api/{JIRA_API_VERSION}/issue/{issue_key}")


def load_processed() -> set:
    if os.path.exists(PROCESSED_LOG):
        with open(PROCESSED_LOG, "r") as f:
            return set(json.load(f))
    return set()


def mark_processed(issue_key: str, processed: set) -> None:
    processed.add(issue_key)
    with open(PROCESSED_LOG, "w") as f:
        json.dump(sorted(processed), f, indent=2)


def description_to_text(description) -> str:
    """Jira Server/Data Center (API v2) usually returns description as a
    plain string. Jira Cloud (API v3) returns it as a nested ADF JSON tree.
    Handle both."""
    if description is None:
        return ""
    if isinstance(description, str):
        return description.strip()

    parts = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                parts.append(node.get("text", ""))
            for child in node.get("content", []):
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(description)
    return " ".join(parts).strip()


# ── Extracting ready-made content straight out of the ticket ───────────
# Tickets in this workflow already contain the finished article: a line
# like "Title : ..." followed by "Description : ..." and then the full
# formatted body (headings, bold text, links, bullet lists). Instead of
# asking Claude to write something new, we pull that straight out of the
# ADF tree and convert it to HTML.

TITLE_LABEL_RE = re.compile(r"^\s*title\s*[:\-]\s*(.+)$", re.IGNORECASE)
DESC_LABEL_RE = re.compile(r"^\s*desc\w*\s*[:\-]\s*", re.IGNORECASE)  # tolerates typos like "Descirption"


def _node_plain_text(node) -> str:
    """Flattens a single ADF node down to its plain text, ignoring marks."""
    parts = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "text":
                parts.append(n.get("text", ""))
            for c in n.get("content", []):
                walk(c)
        elif isinstance(n, list):
            for item in n:
                walk(item)

    walk(node)
    return "".join(parts)


def _adf_marks_to_html(text: str, marks: list | None) -> str:
    html = text
    for mark in marks or []:
        t = mark.get("type")
        if t == "strong":
            html = f"<strong>{html}</strong>"
        elif t == "em":
            html = f"<em>{html}</em>"
        elif t == "code":
            html = f"<code>{html}</code>"
        elif t == "strike":
            html = f"<s>{html}</s>"
        elif t == "underline":
            html = f"<u>{html}</u>"
        elif t == "link":
            href = mark.get("attrs", {}).get("href", "#")
            html = f'<a href="{href}">{html}</a>'
    return html


def _adf_inline_to_html(nodes: list | None) -> str:
    parts = []
    for node in nodes or []:
        t = node.get("type")
        if t == "text":
            parts.append(_adf_marks_to_html(node.get("text", ""), node.get("marks")))
        elif t == "hardBreak":
            parts.append("<br>")
    return "".join(parts)


def _adf_node_to_html(node: dict) -> str:
    t = node.get("type")
    if t == "paragraph":
        inner = _adf_inline_to_html(node.get("content"))
        return f"<p>{inner}</p>" if inner.strip() else ""
    if t == "heading":
        level = node.get("attrs", {}).get("level", 2)
        inner = _adf_inline_to_html(node.get("content"))
        return f"<h{level}>{inner}</h{level}>"
    if t == "bulletList":
        items = "".join(f"<li>{_adf_list_item_to_html(li)}</li>" for li in node.get("content", []))
        return f"<ul>{items}</ul>"
    if t == "orderedList":
        items = "".join(f"<li>{_adf_list_item_to_html(li)}</li>" for li in node.get("content", []))
        return f"<ol>{items}</ol>"
    if t == "blockquote":
        inner = "".join(_adf_node_to_html(c) for c in node.get("content", []))
        return f"<blockquote>{inner}</blockquote>"
    # Unsupported node types (panels, tables, media, etc.) are skipped rather
    # than crashing - fine for a POC, worth revisiting if tickets use them.
    return ""


def _adf_list_item_to_html(list_item: dict) -> str:
    return "".join(_adf_node_to_html(c) for c in list_item.get("content", []))


def extract_title_and_html(description) -> tuple[str | None, str | None]:
    """Looks for a 'Title : ...' line anywhere near the top of the ticket
    description (there may be a leading marker line like 'Update : content'
    before it) and, if found, returns (title, html_body) built from
    everything after it. Returns (None, None) if no 'Title :' line is
    present at all, so the caller can fall back to Claude."""
    if not isinstance(description, dict):
        return None, None

    nodes = description.get("content", [])
    if not nodes:
        return None, None

    title_index, title = None, None
    for i, node in enumerate(nodes):
        match = TITLE_LABEL_RE.match(_node_plain_text(node))
        if match:
            title_index, title = i, match.group(1).strip()
            break

    if title_index is None:
        return None, None

    remaining = nodes[title_index + 1:]
    if remaining:
        first_text = _node_plain_text(remaining[0])
        if DESC_LABEL_RE.match(first_text):
            # Strip just the "Description :" label off the first text run,
            # keeping the rest of that paragraph's content/marks intact.
            node_copy = json.loads(json.dumps(remaining[0]))
            content = node_copy.get("content", [])
            if content and content[0].get("type") == "text":
                content[0]["text"] = DESC_LABEL_RE.sub("", content[0]["text"], count=1)
            remaining[0] = node_copy

    html = "\n".join(filter(None, (_adf_node_to_html(n) for n in remaining)))
    return title, html


def generate_content(summary: str, description: str) -> str:
    prompt = (
        "Write a WordPress blog post based on this content brief.\n\n"
        f"Title/topic: {summary}\n"
        f"Brief: {description}\n\n"
        "Return only the HTML body using <h2> and <p> tags. "
        "No markdown, no preamble, no explanation."
    )
    resp = requests.post(
        "https://gateway.bounteous.tools/v1/messages",
        headers={
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(block["text"] for block in data["content"] if block["type"] == "text")


def _jira_post(path: str, payload: dict) -> dict:
    url = f"{JIRA_BASE_URL}{path}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    if JIRA_AUTH_MODE == "bearer":
        headers["Authorization"] = f"Bearer {JIRA_API_TOKEN}"
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
    else:
        resp = requests.post(url, auth=(JIRA_EMAIL, JIRA_API_TOKEN), headers=headers, json=payload, timeout=30)

    resp.raise_for_status()
    return resp.json() if resp.text else {}


def add_jira_comment(issue_key: str, text: str) -> dict:
    """Comment body format differs: Cloud (v3) wants ADF, Data Center (v2)
    wants a plain string."""
    if JIRA_API_VERSION == "3":
        body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
            }
        }
    else:
        body = {"body": text}
    return _jira_post(f"/rest/api/{JIRA_API_VERSION}/issue/{issue_key}/comment", body)


def get_transitions(issue_key: str) -> list[dict]:
    data = _jira_get(f"/rest/api/{JIRA_API_VERSION}/issue/{issue_key}/transitions")
    return data.get("transitions", [])


def transition_issue(issue_key: str, target_status_name: str) -> bool:
    """Looks up the transition ID matching a status name (e.g. 'Review') and
    fires it. Returns False (without raising) if no matching transition is
    available from the ticket's current status - workflow names vary by
    project, so this is treated as a soft failure."""
    transitions = get_transitions(issue_key)
    match = next((t for t in transitions if t["name"].lower() == target_status_name.lower()), None)
    if not match:
        available = [t["name"] for t in transitions]
        print(f"  No transition named '{target_status_name}' available right now (options: {available})")
        return False
    _jira_post(f"/rest/api/{JIRA_API_VERSION}/issue/{issue_key}/transitions", {"transition": {"id": match["id"]}})
    return True


def _wp_request(method: str, rest_path: str, params: dict | None = None, json_body: dict | None = None) -> dict:
    """Uses the '?rest_route=' query-string form of the WP REST API instead
    of the '/wp-json/' path form. The path form depends on pretty permalinks
    (and working .htaccess rewrite rules), which some free/shared hosts
    like InfinityFree don't reliably support - rest_route works regardless
    of permalink settings.

    Also sends a browser-like User-Agent: some free hosts' bot protection
    silently drops requests carrying default library User-Agent strings
    (curl/x.x, python-requests/x.x), which shows up as an empty reply
    rather than a normal HTTP error."""
    url = f"{WP_BASE_URL}/index.php"
    query = {"rest_route": rest_path}
    if params:
        query.update(params)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    }

    if method == "GET":
        resp = requests.get(url, auth=(WP_USER, WP_APP_PASSWORD), params=query, headers=headers, timeout=30)
    else:
        headers["Content-Type"] = "application/json"
        resp = requests.post(
            url,
            auth=(WP_USER, WP_APP_PASSWORD),
            params=query,
            headers=headers,
            data=json.dumps(json_body or {}),
            timeout=30,
        )
    resp.raise_for_status()
    return resp.json()


def create_wp_post(title: str, html_content: str, status: str = "draft") -> dict:
    return _wp_request(
        "POST", "/wp/v2/posts",
        json_body={"title": title, "content": html_content, "status": status},
    )


def find_wp_post_by_title(title: str) -> dict | None:
    """Searches WordPress (including drafts) for a post whose title matches
    exactly (case-insensitive) - used by 'contentbridge-update-poc' tickets,
    which update an existing post instead of creating a new one."""
    results = _wp_request(
        "GET", "/wp/v2/posts",
        params={"search": title, "status": "any", "per_page": 20},
    )
    for post in results:
        if post.get("title", {}).get("rendered", "").strip().lower() == title.strip().lower():
            return post
    return None


def update_wp_post(post_id: int, html_content: str) -> dict:
    return _wp_request("POST", f"/wp/v2/posts/{post_id}", json_body={"content": html_content})


def process_issue(issue: dict) -> bool:
    """Runs the full chain for one ticket. Returns True only if every step
    succeeded - a partial failure is recorded as a Jira comment and the
    ticket is left unprocessed so the next run retries it."""
    key = issue["key"]
    fields = issue["fields"]
    description = fields.get("description")
    is_update = JIRA_LABEL_UPDATE in (fields.get("labels") or [])
    print(f"\n[{key}] {fields['summary']} ({'update' if is_update else 'create'})")

    try:
        title, html_content = extract_title_and_html(description)

        if title and html_content:
            print("  Ticket already contains the finished article - using it as-is (no Claude generation).")
        else:
            print("  No 'Title :' pattern found - falling back to Claude to draft from the description as a brief.")
            title = fields["summary"]
            description_text = description_to_text(description)
            print(f"  Description: {description_text[:200]}{'...' if len(description_text) > 200 else ''}")
            html_content = generate_content(title, description_text)

        if is_update:
            print(f"  Looking for an existing WordPress post titled '{title}' ...")
            existing_post = find_wp_post_by_title(title)
            if not existing_post:
                raise ValueError(f"No WordPress post found with title '{title}' to update.")
            post = update_wp_post(existing_post["id"], html_content)
            post_id = post.get("id")
            post_link = post.get("link") or f"{WP_BASE_URL}/?p={post_id}"
            print(f"  Updated WP post {post_id}: {post_link}")
            comment_text = (
                f"The content for this ticket has been applied to the existing page. "
                f"Please review the updated page here: {post_link}"
            )
        else:
            print("  Posting to WordPress as a draft ...")
            post = create_wp_post(title, html_content, status="draft")
            post_id = post.get("id")
            post_link = post.get("link") or f"{WP_BASE_URL}/?p={post_id}"
            print(f"  Created WP post {post_id}: {post_link}")
            comment_text = (
                f"Content has been added for this ticket. "
                f"Please review the page here: {post_link}"
            )

        print("  Commenting back on the ticket ...")
        add_jira_comment(key, comment_text)

        print(f"  Moving ticket to {JIRA_TARGET_STATUS} ...")
        transition_issue(key, JIRA_TARGET_STATUS)

        return True

    except Exception as e:
        print(f"  Error processing {key}: {e}")
        try:
            add_jira_comment(key, f"ContentBridge automation failed: {e}")
        except Exception as comment_err:
            print(f"  Also failed to record the error as a comment: {comment_err}")
        return False


def main():
    print(f"Searching Jira for tickets labeled '{JIRA_LABEL_CREATE}' or '{JIRA_LABEL_UPDATE}' ...")
    issues = find_issues_by_labels([JIRA_LABEL_CREATE, JIRA_LABEL_UPDATE])
    processed = load_processed()

    new_issues = [i for i in issues if i["key"] not in processed]
    if not new_issues:
        print("No new tickets to process.")
        return

    print(f"Found {len(new_issues)} new ticket(s): {[i['key'] for i in new_issues]}")
    for issue in new_issues:
        success = process_issue(issue)
        if success:
            mark_processed(issue["key"], processed)
        # if it failed, leave it out of processed_issues.json so the next run retries it


if __name__ == "__main__":
    main()
