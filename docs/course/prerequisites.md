# Prerequisites

A few things need to exist before the course starts, and they involve other
people's systems — an account to create, a key to generate. Do them ahead of
time. None of them are hard, but discovering you need one mid-walkthrough costs
you the walkthrough.

```{nvlearning-meta}
- **Level:** All levels — no prior VLM or Cosmos experience needed
- **When:** Before Part 2.1
- **Time:** About 15 minutes
```

## Learning Objectives

```{nvlearning-objectives}
- **Create** an NVIDIA NGC account and generate a Personal API Key scoped to the NGC Catalog.
- **Store** that key safely, and recognise where the course asks for it.
- **Set up** a command-line coding agent (Codex, Claude Code, OpenClaw, or similar) with an active account.
```

## Create an NGC Personal API Key

Part 2.1 builds the tuned traffic profile, and that build authenticates to
NVIDIA's container registry (`nvcr.io`) to verify the images it uses. That
authentication needs an **NGC Personal API Key** issued to your own account.

:::{important}
Generate this **before** the course. The key is shown exactly once, and the
walkthrough that needs it does not pause while you go and make one.
:::

### 1. Sign in to NGC

Go to [ngc.nvidia.com](https://ngc.nvidia.com) and enter your email address to
log in, or to create an NVIDIA profile if you don't have one yet. Registration
takes a few minutes.

![The NGC log-in page at ngc.nvidia.com/signin, with the Email Address field and Continue button](assets/ngc-login.png)

### 2. Open the API Keys page

Click your **username** in the top right corner and select **Account Settings**
from the dropdown.

![The NGC user dropdown open, with Account Settings highlighted above Setup](assets/ngc-account-settings.png)

On that page, scroll down to **Keys & Secrets** and click **Generate API Key**
in the **API Keys** card.

![The Keys & Secrets section of the Account page, with the Generate API Key button in the API Keys card highlighted](assets/ngc-api-key-section.png)

### 3. Generate the key

Click **+ Generate Personal Key** at the top right of the API Keys page.

![The API Keys page, with the Generate Personal Key button highlighted at the top right](assets/ngc-generate-key-button.png)

Fill in the dialog:

1. **Key Name** — something you'll recognise later, such as `dli-ngc-key`.
2. **Expiration** — the default of 12 months is fine; the course needs it for a day.
3. **Key Permissions → Services Included** — select **NGC Catalog**.
4. Click **Generate Personal Key**.

![The Generate Personal Key dialog: Key Name dli-ngc-key, Expiration 12 months, and NGC Catalog selected under Services Included](assets/ngc-set-key-scope.png)

:::{warning}
**Copy the key immediately.** NGC shows it once and will not show it again. Put
it in a password manager or another secure store before you close the dialog.
If you lose it, generate a new one — you cannot recover the old.
:::

It must be a **Personal Key**, not a legacy NGC API key. Personal Keys are
required for Cosmos 3 Reasoner NIM 1.7.0 and later, which is what this course
deploys.

### 4. Know where the course asks for it

You won't need the key until
[Part 2.1](part-2-1-deploy-zero-shot-vss), where one line of the environment
setup reads it in without echoing it to the screen:

```bash
read -rsp 'NGC Personal Key: ' NGC_CLI_API_KEY && echo && export NGC_CLI_API_KEY NGC_API_KEY="$NGC_CLI_API_KEY"
```

Paste it at that prompt. It lives only in that shell for the length of your
session.

:::{danger}
**Never paste your key into a chat with the coding agent, a notebook cell, or a
file.** The agent never needs to see it — by the time it starts, the key is
already exported in the shell it inherits. If the agent asks you for a
credential, the answer is that the key is already set. Do not commit a key to
version control.
:::

## Set Up a Coding Agent

Every walkthrough in Part 2 is driven by a **command-line coding agent**. The
walkthroughs are written for **Codex** (OpenAI), but you can use any agent you
prefer — Claude Code, OpenClaw, Codex, or others. The agent skills and prompts
work the same way regardless of which you choose.

### Get an Account for Your Agent

Make sure you have a working account and subscription for whichever agent you
plan to use:

- **Codex** — requires a ChatGPT Pro subscription at [chatgpt.com](https://chatgpt.com).
  If you are attending a live NVIDIA AI Day event, a one-month free promo code
  may be provided on a card at the session.
- **Claude Code** — requires an Anthropic account at [claude.ai](https://claude.ai).
- **OpenClaw** or other agents — follow their respective setup instructions.

### Sign In to Codex (if Using Codex)

The course runs on remote machines, so the browser-redirect login does not work.
You use the **device code** flow instead.

1. Run `codex` in your terminal.
2. Choose option **2, "Sign in with Device Code"**. This is the one that works on
   a remote machine.
3. Follow the prompts to log in with your personal email, and **copy the device
   code** it prints. You may hit a security setting at
   [auth.openai.com/codex/device](https://auth.openai.com/codex/device) — the
   next step clears that.
4. Enable device code authorization. In a separate browser tab, go to
   [auth.openai.com/log-in](https://auth.openai.com/log-in) and log in. Then open
   **Settings → Security** and toggle **Enable device code authorization for
   Codex**.

   ![ChatGPT Advanced security settings with Security selected in the sidebar and the Enable device code authorization for Codex toggle switched on](assets/codex-security-toggle.png)
5. Return to [auth.openai.com/codex/device](https://auth.openai.com/codex/device).
   It should now offer **Sign in to Codex with ChatGPT**.
6. Paste the device code from step 3.

   ![The Use your device code to grant access to Codex CLI page, showing the nine-character entry field and a security warning that device codes are a common phishing target](assets/codex-device-code-entry.png)
7. The tab closes itself. Go back to the terminal — you're signed in.

:::{warning}
**Never share a device code.** They are a common phishing target, and the Codex
prompt says so for good reason. Only ever paste one into the OpenAI page you
opened yourself.
:::

:::{note}
Part 2.1 runs `codex login --device-auth`, which jumps straight to the
device-code option rather than showing the menu. Same flow, one less choice.
Signing in now means Part 2.1 starts with a working agent.
:::

```{nvlearning-checkpoint} Before You Start the Course
- You have an NGC account and can sign in at [ngc.nvidia.com](https://ngc.nvidia.com).
- You generated a **Personal API Key** with **NGC Catalog** permissions.
- The key is saved somewhere you can copy it from when Part 2.1 asks.
- Your coding agent account is active and the CLI signs in successfully.
```

## What's Next

With the prerequisites in place, start with
[Part 1: Cosmos Basics](part-1-cosmos-basics).
