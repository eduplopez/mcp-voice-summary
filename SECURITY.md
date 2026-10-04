# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |

The project is pre-1.0, so fixes land on the latest release rather than being
backported.

## Reporting a vulnerability

Please report privately rather than opening a public issue.

- Use GitHub's private reporting: **Security** → **Report a vulnerability** on
  https://github.com/eduplopez/mcp-voice-summary/security/advisories/new

Include what you found, how to reproduce it, and the impact you believe it has.
You can expect an acknowledgement within a few days.

## Scope

This is a local MCP server. It is started by the user's own MCP client as a
child process, opens no ports and listens on nothing. Reports that need a
network attacker will therefore be out of scope.

In scope:

- Data leaving the machine through the `edge` engine beyond what is documented.
- Any way a tool argument could reach a network endpoint, the filesystem or a
  command line.
- Failures of the credential redactor that would let a secret be spoken or
  transmitted.
- Ways a dependency or child process could interfere with the MCP stdio stream.
- Local privilege or file handling problems.

Out of scope:

- Anything requiring the user to hand over an already compromised machine.
- Social engineering.
- Denial of service caused by a local process the user controls.

## Threat model in one paragraph

The intended deployment is single-user and local: the user starts the server and
decides what their assistant says. The realistic risks are a summary leaking
something sensitive out loud or to Microsoft's servers, a dependency writing to
`stdout` and breaking the protocol, and a runaway client flooding the queue.
Those are the areas this project invests in.

## Hardening notes for reviewers

- `VOICE_REDACT=0` disables credential redaction. It is on by default.
- `VOICE_ENGINE=edge` sends the summary text to Microsoft. Use `sapi5` for a
  fully offline setup.
- `VOICE_PLAYER` executes the given program. Only ever set it from your own
  configuration.
- Voice and language are process-global, so two MCP sessions sharing one server
  process can change each other's voice.