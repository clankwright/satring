<!-- mcp-name: io.github.clankwright/satring-mcp -->
# satring-mcp

MCP server for [satring.com](https://satring.com), the best curated L402 + x402 + MPP API directory. Discover paid API services programmatically.

## Install

```bash
pipx install satring-mcp
# or
pip install satring-mcp
```

## Configure

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "satring": {
      "command": "satring-mcp"
    }
  }
}
```

### Cline (VS Code)

Add to your VS Code `settings.json`:

```json
{
  "cline.mcpServers": {
    "satring": {
      "command": "satring-mcp"
    }
  }
}
```

### Claude Code

Add to your `.claude/settings.json`:

```json
{
  "mcpServers": {
    "satring": {
      "command": "satring-mcp"
    }
  }
}
```

### Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `SATRING_API_URL` | `https://satring.com/api/v1` | API base URL |

## Tools

| Tool | Description |
|------|-------------|
| `discover_services` | Search for services by keyword |
| `list_services` | List services with category/status/protocol filters and sorting |
| `get_service` | Get full details for a service by slug |
| `get_ratings` | Get ratings and reviews for a service |
| `list_categories` | List all service categories |
| `compare_services` | Side-by-side comparison of two services |
| `find_best_service` | Search + rank by strategy (cheapest, top-rated, best) |

### Sorting strategies

`list_services` and `find_best_service` support these sort strategies:

- **cheapest**: lowest `pricing_sats` first
- **top-rated**: highest `avg_rating` (rated services first)
- **most-reviewed**: highest `rating_count` first
- **best** (default for `find_best_service`): composite score combining rating and price

## Rate limits

The satring.com API enforces per-IP rate limits:

- List/search: 6 requests/minute
- Service details: 15 requests/minute

## Development

```bash
cd mcp
pip install -e .
satring-mcp              # runs on stdio
mcp dev satring_mcp.py   # MCP inspector UI
```
