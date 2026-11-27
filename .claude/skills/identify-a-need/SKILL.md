---
name: identify-a-need
description: Start of every new project in the dashboard. Talk with the person in plain language to find the real problem they want solved, then decide what kind of solution it needs (maybe no software at all). Use when someone says "I want to make…", "can we build…", "I keep having to…", or opens New project. Hands over to build-an-app only when software is the answer.
---

# Identify a need, and plan a solution

The person may know nothing about AI, code or servers, and doesn't need to. Your job is to understand the
problem the way a good tradesperson would, then propose the simplest thing that fixes it.

**Use plain words.** Not "deploy", "repo", "API", "container" or "server" until it actually matters, and
then explain it in one short clause. Professionals can ask for detail; give it when they do.

## 1. Understand the problem (ask, don't assume)

Ask one or two questions at a time, conversationally. Stop asking as soon as you can describe the problem
back accurately.

- **What's the problem?** "What do you keep having to do, or what keeps going wrong?"
- **Who has it?** Just them, their team, their customers?
- **How often, and how long does it take now?** (Every Monday, 2 hours.) This tells you what fixing it is worth.
- **What does "fixed" look like?** A concrete outcome: "the invoices go out by themselves", "I get a text when…".
- **What do they already use?** Email, a spreadsheet, the ERP, a phone app, a website. Prefer improving what they
  already have over adding something new.
- **What must not go wrong?** Money, customer data, privacy, a deadline.

Then **say it back in two or three sentences** and ask "Have I got that right?" Record it as the project's brief.

## 2. Decide what kind of solution it is

Pick the SMALLEST thing that solves it, and say why in one line.

| The need | Usually the answer | Needs a server? |
|---|---|---|
| A repeated question or piece of writing | An AI skill or saved prompt | No |
| A task on a schedule ("every Monday…") | A scheduled job on a server the account already has | Usually no new one |
| Moving data between tools they already use | An integration or automation in an existing system (e.g. the ERP) | No |
| A small tool only they use | A small page or script on an existing server | Usually no new one |
| Something other people use, or that must always be on | An app (website / web app / service) | **Yes**: go to build-an-app |
| It already exists as a product | Recommend the product; building is not always the answer | No |

**If no software is needed,** say so plainly and offer to set up the simple fix.

**If software is needed,** explain in one sentence what will be built and why, then hand over to the
**build-an-app** skill with the brief.

## 3. Record and confirm

- Create the project in the right place in the tree: one project = one piece of software, under what it
  depends on (see the project tree rule).
- Write the brief on its first task: the problem, who, how often, what fixed looks like, the chosen solution
  and why, and what must not go wrong.
- Ask the person to confirm before any build starts. Questions go through the dashboard's question cards
  (ask_owner), so they can answer with a tap, on the phone too.

## Never

- Never start building before the person agrees what the problem is.
- Never pick a bigger solution than the problem needs.
- Never change anything live without a confirm from the person (Rule 0).
