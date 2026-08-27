# ContentBridge

Automated pipeline connecting **Jira** to **WordPress**, with **Claude** as a
content-generation fallback. Runs on a schedule via GitHub Actions.

## What it does

1. Searches Jira for tickets in **To Do** status tagged with one of two labels
2. Reads the ticket's title and content:
   - If the description already contains a `Title :` line, uses that
     content as-is (no AI involved)
   - Otherwise, falls back to Claude to draft a post from the ticket as a brief
3. Creates or updates a WordPress post, depending on the label
4. Comments the WordPress link back on the ticket and moves it to
   **Code Review**

## Labels

| Label | Behavior |
|---|---|
| `contentbridge-poc` | Creates a new WordPress draft |
| `contentbridge-update-poc` | Finds an existing post by matching title and updates its content |

## Ticket format
