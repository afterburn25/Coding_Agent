# Architecture

## Core Layers

1. **UI** — local ChatGPT-style interface plus runtime and task workflow panels.
2. **Autonomy** — mission management, budgeting, and recovery logic.
3. **Permissions** — access control and audit tracking.
4. **Action Ledger** — persistent record of all consequential actions.
5. **Answer Memory** — learned Q&A over local experience.
6. **Tool Management** — tool registration, execution, and verification.

## Engineering Missions Layer

The Engineering Missions layer is responsible for defining, tracking, and reporting on engineering missions within Nexus Core. It includes:
- A mission model and durable storage (`MissionStore`).
- An API endpoint for retrieving mission reports (`GET /api/missions/<id>/report`).
- A UI panel for displaying mission reports on the missions detail page.
- Tests for the payload shape of mission reports.
- Documentation of the Engineering Missions layer in this file.

## Mission Report Endpoint

The `GET /api/missions/<id>/report` endpoint returns a compact human-readable summary of a mission, including:
- Title
- Status
- Per-workstream progress
- Metrics

This endpoint is implemented in the `localcodeagent/autonomy/missions.py` file and is tested in `tests/test_end_to_end.py`.

## Testing

The payload shape of the mission report is tested in `tests/test_end_to_end.py` to ensure it meets the expected structure.

## Conclusion

The Engineering Missions layer provides a structured way to define, track, and report on engineering missions within Nexus Core. It ensures that missions are well-documented, easily accessible, and verifiable through the provided API and UI components.