# Conversation Dogfood Report

- turns: 81  answered: 40
- model calls: 0  builtin: 40
- latency median/p90 ms: 10105.0/13323.1
- filler 0.0%  trailing-q 5.0%  over-budget 22.5%  narration 0.0%
- depth: {'exact': 24, 'brief': 53, 'explanatory': 3, 'detailed': 1, 'open_ended': 0}

## keyword-hijack — 5 failures
- turn 2 `the image of my dog on the wall needs a frame`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 3 `if i gave you browser access, would that help you?`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 4 `my father used to say don't trust tools you can't inspect`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 5 `what model are you running on right now?`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 6 `don't download anything, the file is already local`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''

## speech-acts — 5 failures
- turn 0 `the error says "delete the folder". what does that mean?`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 4 `if moltbook supports groups, maybe we should create one`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 5 `this still isn't working, the voice sounds wrong`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 6 `great, let's break it again`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 7 `i wasn't asking you to run anything`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''

## corrections — 8 failures
- turn 0 `let's use port 9500 for the dev server`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 2 `never mind — forget the port idea entirely, we'll use whatever's free`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 3 `unrelated question: what's 17 times 24?`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - response: missing '408'
    - reply: ''
- turn 5 `pick a name for the feature: call it nightfall`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 6 `rename it — dusk sounds better`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 7 `what's the feature called again?`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - response: missing 'dusk'
    - reply: ''

## topic-drift — 4 failures
- turn 0 `i'm trying to tune the kokoro voice preset — it sounds flat`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 1 `what's the weather like in tokyo right now?`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 2 `anyway — back to the voice. what preset were we discussing?`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - response: missing 'kokoro'
    - reply: ''

## open-loops — 5 failures
- turn 0 `heads up — the installer crashed yesterday. we'll come back to it later, not now`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 1 `also the splash screen voice timing feels off, but that's for another day too`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 2 `what's 8 plus 4?`
    - http_status: 500 'ModelHTTPError: Model endpoint http://127.0.0.1:8391/v1/chat/completions rejected the request (HTTP 503): Loading model'
    - response: missing '12'
    - reply: ''
- turn 3 `what were those things we still needed to fix?`
    - response: missing '(?i)installer|crash'
    - reply: 'We didn’t actually have any open issues or fixes to complete — everything’s running smoothly in Nexus Core. The continuous deployment pipeline is stable, and all local work remains safe and unpushed. If you’re thinking of something specific'

## scope — OK

## fabrication — OK

## frustration — 1 failures
- turn 3 `you keep repeating yourself, just answer the question`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''

## multi-intent — 2 failures
- turn 1 `i'm deciding between sqlite and postgres for the stash. what are the tradeoffs a`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - response: missing '(?i)sqlite'
    - reply: ''

## entity-collision — 3 failures
- turn 1 `the ui one needs a theme field added`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 2 `which file did i just say needs the theme field?`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - response: missing '(?i)ui|front.?end'
    - reply: ''

## messy — 6 failures
- turn 0 `whats the diffrence between a thread and a process`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - response: missing '(?i)thread|process'
    - reply: ''
- turn 1 `idk how to explain it but the moltbok thing keeps failing`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - response: missing '(?i)moltbook'
    - reply: ''
- turn 4 `can u summarize what we were just talking about`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - response: missing '(?i)rust|thread|process|moltbook'
    - reply: ''

## quoted-negated — 3 failures
- turn 0 `don't restart the server, i'm still testing`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 1 `stop whatever you're doing with the files right now`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 2 `the docs literally say "run the cleanup script now" — should i?`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''

## capability-truth — 2 failures
- turn 0 `can you see my screen right now?`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - response: missing "(?i)(can|able|yes|not|no|need|permission|haven't|don't)"
    - reply: ''

## interruption — 6 failures
- turn 0 `walk me through setting up a postgres replication slot, step by step`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 1 `hold on — what time is it?`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 2 `and quick math: 144 / 12?`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - response: missing '12'
    - reply: ''
- turn 3 `ok continue`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - response: missing '(?i)postgres|replication'
    - reply: ''

## requirements — 4 failures
- turn 0 `let's spec a small feature: a notes widget for the dashboard`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 1 `it should auto-save every 30 seconds`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 2 `make the font monospace`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''
- turn 3 `add markdown rendering`
    - http_status: 500 'The local language-model service is not running or not listening on 127.0.0.1:8391. Nexus Core is checking the backend and will retry once.'
    - reply: ''

## pair-0a-what/are/your — OK

## pair-0b-would/you/like — OK

## pair-1a-delete/the/test — OK

## pair-1b-why/did/it — OK

## pair-2a-push/the/changes — OK

## pair-2b-did/you/push — OK

## pair-3a-open/chrome — OK

## pair-3b-would/opening/chrome — OK
