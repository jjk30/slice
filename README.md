# slice

slice is a tool that sits between your apps and AI providers like Anthropic, and quietly makes your AI bill smaller.

You point your app at slice instead of straight at the AI. Everything still works exactly the same. The difference is that slice watches every request, sends the easy ones to cheaper AI models, remembers answers it has seen before so you don't pay twice, and stops the spending when you hit a limit you set.

Change one line in your app, and your AI bill drops.

## Why this exists

In 2026, teams at companies like Uber and Microsoft started using AI tools heavily. The tools were great, so people used them a lot. The problem was that AI is billed by usage with no ceiling, and nobody could see who was spending what or hit the brakes in time. The bills blew up.

slice is the meter and the brake on that pipe. It never reads your code or your data. It only sees the AI traffic passing through it, and even that can stay entirely inside your own servers.

## What it does

slice does four things, each one a way to spend less or stay in control.

**Forwards your requests.** Your app talks to slice, slice talks to the AI, and the answer comes straight back. From your app's point of view nothing changed.

**Picks a cheaper model.** Before sending each request, slice quickly checks how hard the task is. Easy questions go to a cheap model, hard ones go to a strong model. You pay the high price only when you actually need it.

**Reuses old answers.** If the same request comes in twice, slice gives back the answer it already has instead of paying the AI again.

**Caps the spending.** You set a budget per team. slice keeps a running total, warns you as you get close, and blocks new requests once the limit is reached. This is the brake the Uber and Microsoft teams wished they had.

It also keeps a record of every request (which model, how long it took, how many tokens, how much it cost) so you can see exactly where the money goes.

## How your app connects

Point any AI tool at slice and keep using your own AI key. slice never stores or needs a key of its own. Your key stays yours.

```bash
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```

That one line works in a terminal, in your editor, in production, anywhere.

## How to run it

You need Docker and Node installed. Then from the project folder:

```bash
cd gateway
cp .env.example .env          # copy the settings file (dev defaults are fine)
docker compose up -d          # starts the database and cache
npm install && npm start      # starts slice on port 8080
```

slice is now running. Send it a test request with your own AI key in place of the placeholder.

```bash
curl http://localhost:8080/v1/messages \
  -H "content-type: application/json" \
  -H "x-api-key: YOUR_KEY_HERE" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-opus-4-8","max_tokens":64,"messages":[{"role":"user","content":"hi"}]}'
```

You will get a normal AI reply back, and slice will have logged the request behind the scenes.

## What's built so far

| Step | What it adds |
|------|--------------|
| 1 | Forwards every request to the AI and keeps a log of each one |
| 2 | Saves those logs to a database so they survive restarts |
| 3 | Picks the cheapest model that can handle each request |
| 4 | Reuses old answers and caps spending per team |

Still to come: a dashboard to see everything at a glance, smarter model recommendations that learn from your own usage, alerts by email and Slack, and a one-click deploy to the cloud.

## How it's put together

```
your app  ──▶  slice  ──▶  the AI provider
                 │
        check budget ▶ check cache ▶ pick model ▶ send ▶ save answer ▶ record cost
                 │                                            │
              cache + running totals                     request history
```

All the code lives in the `gateway` folder. Each file does one job: forwarding, model picking, caching, budgets, pricing, logging, and talking to the database and cache.

## A few things worth knowing

You never give slice an AI key. Your apps bring their own. The settings file you actually use (`.env`) is kept private and never shared; only the example file with fake values is public.

slice is built to stay up. If the database or cache goes down, slice keeps serving your requests anyway and just notes the problem in its logs. A broken logging system can never take down your AI traffic.

slice can run entirely on your own machines, so your data never has to leave your network.
