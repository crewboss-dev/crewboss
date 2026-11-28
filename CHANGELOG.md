# Changelog

## 0.1.1

New
- One **Run** button on every task. Crewboss picks the runner for you (online, right labels, least busy); a small settings popover changes the profile, labels or runner.
- **Runners for this project** in the project's Settings: tick which runners may take its jobs.
- An **outcome card** after each job: what happened in plain words, the checks run, next steps and remaining work, with **Review the change** or **Run again** (which carries on from the last branch).
- A **live runner view**: watch what the agent says and does while it works (watch only).
- A new **task window**: a Now line, a three-line Summary (What / Done / Left, kept current by the AI and editable by you) and one Timeline of comments, questions and runs.
- A job waiting for you to review agent files now shows as "waiting on you" instead of failing, and carries on once you pin them.
- **Trust these files and run** on the task itself: the project owner reads every agent file the job would load (files that can run things open first) and trusts exactly those in one click. Files that arrive after the page opened are never trusted with them.
- **Mark done** when the last run finished the work, and the assistant can close or move a task (you confirm on a card).
- "Use my runner for task summaries" lives in your account's **Preferences** (every sign-in mode).

Fixed
- Empty files among the agent files (an empty `.gitkeep`, say) no longer block a job for ever.
- A failed run shows one main button, not two; long task titles wrap; failure text is cut at a word, not mid-word.
- Public docs and the tour were brought in line with the product (the old "Run on a PC" wording is gone).
- The release leak check now also compares values, not only file names, so private values cannot slip into a public push.
