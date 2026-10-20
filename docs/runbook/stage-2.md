# Stage 2: create and authorize the Slack app

Prerequisites:

- permission to create or approve an app in the target Slack workspace;
- one destination channel ID for the fallback route;
- any additional destination channel IDs needed by routing rules.

Use a dedicated Slack app for this deployment. Reusing an app couples its
credential lifecycle and channel access to every other consumer.

In <https://api.slack.com/apps>:

1. Create a dedicated Slack app in the target workspace.
2. Under **OAuth & Permissions**, add the bot scope `chat:write`.
3. In **OAuth & Permissions**, confirm token rotation is disabled. Do not enable
   it for this application.
4. Install or reinstall the app to the workspace.
5. Store the resulting `xoxb-` bot token in an approved password manager.
6. Invite the bot to every destination channel.
7. Open each channel's details and record its channel ID.

Expected state: you have one `xoxb-` token and every configured channel shows
the bot as a member. Token rotation is disabled. The service does not need a
signing secret, event subscription, slash command, or inbound app manifest.

Slack's [token rotation documentation](https://docs.slack.dev/authentication/using-token-rotation/)
states that rotating access tokens expire every 12 hours and must be refreshed
with a refresh token. This runtime stores one static `xoxb-` value and has no
OAuth client-secret or refresh-token flow. It does not support rotating
`xoxe.xoxb-*` access tokens.

Checkpoint: do not continue with only channel names. Routing requires channel
IDs, the bot must already be a channel member when using only `chat:write`, and
token rotation must be disabled. If an existing app has token rotation enabled,
stop and create a dedicated app without it. Slack states that token rotation
cannot be disabled after it is enabled.

Recovery:

- If app installation is blocked, request workspace administrator approval.
- If a channel is private, have a member invite the bot.
- If you changed scopes after installation, reinstall the app before using the
  new token.
- If the app is shared, record every owner and consumer before proceeding.
  Prefer replacing it with a dedicated app for this deployment.

---

[Previous: Stage 1](stage-1.md) | [Next: Stage 3](stage-3.md)
