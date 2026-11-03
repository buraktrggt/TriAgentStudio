"""Built-in prompt templates."""

TEMPLATES = {
    "Feature development (design -> build -> review)": {
        "shared": (
            "We are working on the project in the selected workspace.\n"
            "- Follow the existing code style and conventions.\n"
            "- Keep changes minimal, focused and well tested.\n"
            "- Never commit, push, or delete user data.\n"
            "- Explain important decisions in your report summary."),
        "claude": (
            "You are the architect and lead reviewer.\n"
            "- Produce clear designs: modules, interfaces, data flow, edge cases.\n"
            "- When fixing review findings, address root causes, not symptoms."),
        "codex": (
            "You are the implementation specialist.\n"
            "- Implement exactly what the design specifies; ask via messages if unclear.\n"
            "- Add or update tests next to the code you change.\n"
            "- Publish a precise change summary (files, functions, behaviour)."),
        "antigravity": (
            "You are the independent reviewer and verifier.\n"
            "- Inspect the real files, not just summaries.\n"
            "- Report concrete defects with file and line references.\n"
            "- Verify behaviour against the objective; approve only when correct."),
    },
    "Code review / audit only (read-only)": {
        "shared": ("Audit the workspace. This is a READ-ONLY engagement: do not modify any files. "
                   "Report findings with severity (critical/high/medium/low), file and line."),
        "claude": "Focus on architecture, correctness and maintainability.",
        "codex": "Focus on implementation bugs, error handling and test coverage gaps.",
        "antigravity": "Focus on security, dependency risks and verification of claims made by "
                       "the other reviewers.",
    },
    "Bug investigation": {
        "shared": ("Investigate the bug described in the objective. Reproduce it, find the root "
                   "cause, fix it with a regression test, and verify the fix."),
        "claude": "Lead the root-cause analysis and propose the fix strategy.",
        "codex": "Implement the fix and the regression test.",
        "antigravity": "Independently reproduce the bug and verify the fix does not regress "
                       "anything else.",
    },
    "Documentation": {
        "shared": "Improve the project's documentation. Keep it accurate to the actual code.",
        "claude": "Write conceptual/architecture documentation and the README structure.",
        "codex": "Write API reference and usage examples verified against the code.",
        "antigravity": "Review all documentation for accuracy against the code and clarity.",
    },
}
