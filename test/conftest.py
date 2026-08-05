import os


os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault(
    "AZURE_TABLE_ENDPOINT", "https://example.table.core.windows.net")
os.environ.setdefault(
    "SERVICE_HEALTH_ROUTES_JSON",
    '{"default_channel_id": "C0123456789", "rules": []}',
)
