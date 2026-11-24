# Sleep and memory: how Somni's cycles relate to human sleep

Somni borrows its vocabulary from sleep research: recording, daydreams, dreams, deep sleep. This page explains what
each borrowed idea refers to in people (and in the rats and songbirds where much of the work was done), and where
Somni's version differs. It is an analogy, not a model of the brain: Somni is a set of text passes over a transcript,
and none of its design depends on the neuroscience being right.

## The human night, briefly

Sleep runs in cycles of roughly 90 minutes. Each cycle moves through light non-REM sleep (N1, N2), deep slow-wave sleep
(N3), and rapid-eye-movement (REM) sleep. Slow-wave sleep dominates the first half of the night and REM the second. A
leading account of what sleep does for memory is *active systems consolidation* (Diekelmann & Born, 2010; Rasch & Born,
2013). The hippocampus records the day's episodes quickly. During non-REM sleep it replays them, timed with cortical
slow oscillations and sleep spindles, and that replay gradually strengthens a slower, more general representation in the
neocortex. The two-system idea goes back to McClelland, McNaughton & O'Reilly (1995): one system learns fast and keeps
detail, the other learns slowly and keeps structure.

## Somni's cycles, one by one

| Somni | Human counterpart | What it does in Somni |
|---|---|---|
| Recording | Waking encoding (hippocampus) | Keep a verbatim, ordered record of the session |
| Daydream / dream step | Awake replay, mind-wandering | Note lessons while the work goes on, unverified |
| Night dream (`/somni-dream`) | Non-REM systems consolidation | Replay the day, check lessons against evidence, keep what holds |
| Wiki (`/somni-wiki`) | Schema building, integration | Gather claims into pages that link what's known |
| Deep sleep (`/somni-deepsleep`) | Sleep-dependent skill learning | Build and rehearse one procedure (an action word) |
| Forgetting old records *(planned)* | Synaptic downscaling, gist over detail | Let raw detail fade once it's been consolidated |

### Recording: waking encoding

While you work, Somni writes a transcript: what was said, what tools returned, what was shown. This is the
hippocampus's role in the two-system picture: fast, detailed and in order, kept long enough to be reviewed later.
Unlike a hippocampus, the transcript is exact and hash-checked; people's episodic memory is reconstructive and drifts.

### Daydreams and dream steps: awake replay

Replay isn't limited to sleep. In rats resting between runs, hippocampal cells replay the paths just taken, sometimes
in reverse (Foster & Wilson, 2006), and sometimes paths from elsewhere entirely (Karlsson & Frank, 2009). In people,
brain activity during quiet rest after learning predicts how well it is remembered later (Tambini, Ketz & Davachi,
2010), and mind-wandering is now studied as a form of spontaneous thought rather than idle noise (Christoff et al.,
2016).

A Somni daydream is that quiet-rest replay: a short pass while the session keeps working. A dream step is a fork of the
session that only writes the lessons down, marked as unverified. That resembles the idea of *tagging* (Frey & Morris,
1997): something marks a recent memory as worth consolidating, and the heavy lifting happens later. Where the analogy
breaks: a brain can't fork itself, and in swarm mode Somni shares one agent's lessons with others working at the same
time, which has no counterpart in a single brain.

### The night dream: non-REM consolidation

Hippocampal replay during sleep was first shown by Wilson & McNaughton (1994): cells that fired together while a rat
explored fired together again in the sleep that followed. Somni's night dream is a deliberate version of that. It
replays the day's records, takes the day's unverified lessons, checks each one against the records, keeps those the
evidence supports as verified claims, and drops or corrects the rest. Here Somni does something brains don't: it
checks its memories against an exact record. Human consolidation strengthens what is replayed, true or not, which is
one reason false memories can feel as real as true ones.

### The wiki: schemas

New information is consolidated faster when it fits knowledge that is already organized (Tse et al., 2007), and replay
of overlapping memories during sleep is thought to build those organized structures, or schemata (Lewis & Durrant,
2011). REM sleep in particular has been linked to finding new associations between things learned separately (Cai et
al., 2009). Somni's wiki pass is the schema step: it gathers claims into pages about one subject, linked to each other.

### Deep sleep: learning procedures

Skills improve between sessions, and sleep helps. Walker and colleagues (2002) had people learn a finger-tapping
sequence; after a night's sleep they were faster and more accurate without further practice, and the gain tracked
the amount of stage 2 non-REM sleep late in the night. Songbirds replay song motor patterns during sleep (Dave &
Margoliash, 2000). Habits, the procedures we run without thinking, depend on the basal ganglia rather than the
hippocampus (Graybiel, 2008).

`/somni-deepsleep` is Somni's procedural cycle. It picks one chore the user repeats, or one task that failed because
step-by-step calls were too slow, and builds an action word for it: a short program that runs the whole sequence in one
step. Two caveats on the name. In sleep research, "deep sleep" usually means slow-wave sleep (N3), which is more
associated with remembering facts and events than with skills; the motor-skill gains above track lighter, stage 2
sleep. And people don't act out procedures while asleep (the body is mostly paralysed in REM). Somni's deep sleep, by
contrast, may call real tools to rehearse a draft before keeping it. The name is a nod, not a claim.

### Forgetting: keeping the gist

Sleep also cuts back. The synaptic homeostasis hypothesis (Tononi & Cirelli, 2014) proposes that sleep scales down the
connections strengthened during the day, keeping the strongest and letting the rest fade, which frees capacity and
keeps the signal. Over time episodic memories also lose their detail and keep their gist. Somni plans the same for raw
transcripts: once a dream has reviewed a stretch of records and kept what mattered as claims, the raw text can expire,
leaving the claims, their quotes, and a placeholder that keeps the record chain intact.

## Action words, habits, tricks and reflexes

Somni's learned procedures go by several names: action words, habits, tricks, reflexes. They are interchangeable and
just for fun; none ranks above another. The names do have neuroscience meanings, though. A habit is a learned,
automatic routine. A reflex, strictly, is an innate response that is never learned at all. So read "reflex" as "fast
enough to need no thought", which is what a word that chains tool calls back to back is for.

## Where the analogy breaks

- Somni's passes are explicit and readable: every claim cites the records it came from.
- Somni checks memories against exact evidence; human memory reconstructs.
- Somni has no sleep pressure. It dreams when asked or on a schedule, and a pass costs tokens, not rest.
- Somni can fork a session, share lessons across agents, and act during deep sleep. A brain can do none of these.

## References

- Cai, D. J., Mednick, S. A., Harrison, E. M., Kanady, J. C., & Mednick, S. C. (2009). REM, not incubation, improves
  creativity by priming associative networks. *PNAS*, 106, 10130–10134.
- Christoff, K., Irving, Z. C., Fox, K. C. R., Spreng, R. N., & Andrews-Hanna, J. R. (2016). Mind-wandering as
  spontaneous thought: a dynamic framework. *Nature Reviews Neuroscience*, 17, 718–731.
- Dave, A. S., & Margoliash, D. (2000). Song replay during sleep and computational rules for sensorimotor vocal
  learning. *Science*, 290, 812–816.
- Diekelmann, S., & Born, J. (2010). The memory function of sleep. *Nature Reviews Neuroscience*, 11, 114–126.
- Foster, D. J., & Wilson, M. A. (2006). Reverse replay of behavioural sequences in hippocampal place cells during the
  awake state. *Nature*, 440, 680–683.
- Frey, U., & Morris, R. G. M. (1997). Synaptic tagging and long-term potentiation. *Nature*, 385, 533–536.
- Graybiel, A. M. (2008). Habits, rituals, and the evaluative brain. *Annual Review of Neuroscience*, 31, 359–387.
- Karlsson, M. P., & Frank, L. M. (2009). Awake replay of remote experiences in the hippocampus. *Nature
  Neuroscience*, 12, 913–918.
- Lewis, P. A., & Durrant, S. J. (2011). Overlapping memory replay during sleep builds cognitive schemata. *Trends in
  Cognitive Sciences*, 15, 343–351.
- McClelland, J. L., McNaughton, B. L., & O'Reilly, R. C. (1995). Why there are complementary learning systems in the
  hippocampus and neocortex. *Psychological Review*, 102, 419–457.
- Rasch, B., & Born, J. (2013). About sleep's role in memory. *Physiological Reviews*, 93, 681–766.
- Tambini, A., Ketz, N., & Davachi, L. (2010). Enhanced brain correlations during rest are related to memory for recent
  experiences. *Neuron*, 65, 280–290.
- Tononi, G., & Cirelli, C. (2014). Sleep and the price of plasticity. *Neuron*, 81, 12–34.
- Tse, D., Langston, R. F., Kakeyama, M., Bethus, I., Spooner, P. A., Wood, E. R., Witter, M. P., & Morris, R. G. M.
  (2007). Schemas and memory consolidation. *Science*, 316, 76–82.
- Walker, M. P., Brakefield, T., Morgan, A., Hobson, J. A., & Stickgold, R. (2002). Practice with sleep makes perfect:
  sleep-dependent motor skill learning. *Neuron*, 35, 205–211.
- Wilson, M. A., & McNaughton, B. L. (1994). Reactivation of hippocampal ensemble memories during sleep. *Science*,
  265, 676–679.
