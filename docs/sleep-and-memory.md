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
| Forgetting old records | Synaptic downscaling, gist over detail | Let raw detail fade once it's been consolidated |
| Strength: fresh, fading, faint | Forgetting curves, retrieval practice | Rank and label claims by how recently they were used |
| `update`: true until it changed | Reconsolidation, memory updating | Keep an outdated claim as history, not as a mistake |

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
keeps the signal. Forgetting is increasingly seen as useful rather than a failure: Richards & Frankland (2017) argue
that memory's job is to guide decisions, not to keep the past, so letting go of detail helps it generalize and adapt
when the world changes. Hardt, Nader & Nadel (2013) describe forgetting as an active process the brain runs on
purpose. Over time episodic memories also lose their verbatim detail and keep their gist. Fuzzy-trace theory (Reyna &
Brainerd, 1995) holds that people store both traces at once and that the verbatim one fades faster.

Somni does the same with raw transcripts. After a night dream, records older than a month keep only a stub: the
opening of their text (the gist), every quote a claim cites from them, and their place in the hash chain. The claims
built from them stay. See [Memory over time](memory-over-time.md#forgetting-old-records).

## Memory over time

### Strength: forgetting curves and retrieval practice

Ebbinghaus (1885) measured how quickly memorized syllables were lost. Later work found that forgetting follows a power
of time rather than an exponential (Wixted & Ebbesen, 1991). Anderson & Schooler (1991) showed that this curve matches
the environment: how likely a fact is to be needed again falls off with time since its last use in the same way, and
rises with how often it has been used. The ACT-R theory of cognition (Anderson et al., 2004) turns this into its
base-level activation, the log of a sum of power-decaying traces, one per use. Being recalled strengthens a memory more
than seeing it again (the testing effect; Roediger & Karpicke, 2006), and emotionally significant events are
consolidated more strongly (McGaugh, 2004).

Somni scores each claim with ACT-R's formula. Every use counts:

- the records it cites;
- the dream that wrote or promoted it;
- each later mention of the claim by ID, which is the agent recalling and using it.

Salience adds a little, as significance does. Bjork & Bjork (1992) separate a memory's *storage strength* (how well
it's learned) from its *retrieval strength* (how easy it is to reach right now), which can fade while storage stays.
Somni's labels are about retrieval only. A faint claim isn't deleted or doubted; it ranks lower and is flagged so the
agent checks it before relying on it.

### Conflicts: reconsolidation and dating memories

When a memory is recalled, it can become changeable for a while and be stored again in a new form. This is
*reconsolidation*, first shown for fear memories in rats (Nader, Schafe & LeDoux, 2000). One account of what it's for
is keeping memories relevant as the world changes (Lee, 2009). The trigger appears to be a mismatch between what was
expected and what happened (Sinclair & Barense, 2019). An older memory can also get in the way of a newer one, which is
called proactive interference (Underwood, 1957).

People keep "both true at different times" apart by context more than by a clock. In the temporal context model
(Howard & Kahana, 2002), each memory is bound to a slowly drifting context, and source monitoring is how people judge
where and when a memory came from (Johnson, Hashtroudi & Lindsay, 1993).

Somni makes those steps explicit. Every record carries its time. The night dream sees current claims next to the dated
records that may contradict them. It has to choose: did the world change (`update`, which keeps the old claim as true
until then), or was the claim wrong (`correct` or `retract`)? An update must cite a record newer than all of the old
claim's evidence. That is the mismatch that, in reconsolidation, opens a memory to change.

### Outcomes: feedback from outside

Feedback teaches the most when it contradicts a confident answer. Errors made with high confidence are corrected more
often than low-confidence ones once the right answer is shown, the *hypercorrection effect* (Butterfield & Metcalfe,
2001). [Outcomes](outcomes.md) give Somni that feedback: what happened after the agent was done, recorded from
outside. A dream sees each one beside the agent's own report, flagged when the agent claimed success and the outcome
says it failed. They are also a source distinction (Johnson, Hashtroudi & Lindsay, 1993): what the agent concluded is
kept apart from what the world said.

## Action words, habits, tricks and reflexes

Somni's learned procedures go by several names: action words, habits, tricks, reflexes. They are interchangeable and
just for fun; none ranks above another. The names do have neuroscience meanings, though. A habit is a learned,
automatic routine. A reflex, strictly, is an innate response that is never learned at all. So read "reflex" as "fast
enough to need no thought", which is what a word that chains tool calls back to back is for.

## Where the analogy breaks

- Somni's passes are explicit and readable: every claim cites the records it came from.
- Somni checks memories against exact evidence; human memory reconstructs.
- Somni has no sleep pressure. It dreams when asked or on a schedule, and a pass costs tokens, not rest.
- Recalling a memory in Somni never rewrites it; only a dream changes a claim, and its earlier versions stay readable.
  In people, reconsolidation can alter a memory without leaving the old version behind.
- Somni can fork a session, share lessons across agents, and act during deep sleep. A brain can do none of these.

## References

- Anderson, J. R., & Schooler, L. J. (1991). Reflections of the environment in memory. *Psychological Science*, 2,
  396–408.
- Anderson, J. R., Bothell, D., Byrne, M. D., Douglass, S., Lebiere, C., & Qin, Y. (2004). An integrated theory of
  the mind. *Psychological Review*, 111, 1036–1060.
- Bjork, R. A., & Bjork, E. L. (1992). A new theory of disuse and an old theory of stimulus fluctuation. In A. F.
  Healy, S. M. Kosslyn & R. M. Shiffrin (Eds.), *From learning processes to cognitive processes* (Vol. 2, pp. 35–67).
  Erlbaum.
- Butterfield, B., & Metcalfe, J. (2001). Errors committed with high confidence are hypercorrected. *Journal of
  Experimental Psychology: Learning, Memory, and Cognition*, 27, 1491–1494.
- Cai, D. J., Mednick, S. A., Harrison, E. M., Kanady, J. C., & Mednick, S. C. (2009). REM, not incubation, improves
  creativity by priming associative networks. *PNAS*, 106, 10130–10134.
- Christoff, K., Irving, Z. C., Fox, K. C. R., Spreng, R. N., & Andrews-Hanna, J. R. (2016). Mind-wandering as
  spontaneous thought: a dynamic framework. *Nature Reviews Neuroscience*, 17, 718–731.
- Dave, A. S., & Margoliash, D. (2000). Song replay during sleep and computational rules for sensorimotor vocal
  learning. *Science*, 290, 812–816.
- Diekelmann, S., & Born, J. (2010). The memory function of sleep. *Nature Reviews Neuroscience*, 11, 114–126.
- Ebbinghaus, H. (1885). *Über das Gedächtnis*. Duncker & Humblot. (English: *Memory: A contribution to experimental
  psychology*, 1913.)
- Foster, D. J., & Wilson, M. A. (2006). Reverse replay of behavioural sequences in hippocampal place cells during the
  awake state. *Nature*, 440, 680–683.
- Frey, U., & Morris, R. G. M. (1997). Synaptic tagging and long-term potentiation. *Nature*, 385, 533–536.
- Graybiel, A. M. (2008). Habits, rituals, and the evaluative brain. *Annual Review of Neuroscience*, 31, 359–387.
- Hardt, O., Nader, K., & Nadel, L. (2013). Decay happens: the role of active forgetting in memory. *Trends in
  Cognitive Sciences*, 17, 111–120.
- Howard, M. W., & Kahana, M. J. (2002). A distributed representation of temporal context. *Journal of Mathematical
  Psychology*, 46, 269–299.
- Johnson, M. K., Hashtroudi, S., & Lindsay, D. S. (1993). Source monitoring. *Psychological Bulletin*, 114, 3–28.
- Karlsson, M. P., & Frank, L. M. (2009). Awake replay of remote experiences in the hippocampus. *Nature
  Neuroscience*, 12, 913–918.
- Lee, J. L. C. (2009). Reconsolidation: maintaining memory relevance. *Trends in Neurosciences*, 32, 413–420.
- Lewis, P. A., & Durrant, S. J. (2011). Overlapping memory replay during sleep builds cognitive schemata. *Trends in
  Cognitive Sciences*, 15, 343–351.
- McClelland, J. L., McNaughton, B. L., & O'Reilly, R. C. (1995). Why there are complementary learning systems in the
  hippocampus and neocortex. *Psychological Review*, 102, 419–457.
- McGaugh, J. L. (2004). The amygdala modulates the consolidation of memories of emotionally arousing experiences.
  *Annual Review of Neuroscience*, 27, 1–28.
- Nader, K., Schafe, G. E., & LeDoux, J. E. (2000). Fear memories require protein synthesis in the amygdala for
  reconsolidation after retrieval. *Nature*, 406, 722–726.
- Rasch, B., & Born, J. (2013). About sleep's role in memory. *Physiological Reviews*, 93, 681–766.
- Reyna, V. F., & Brainerd, C. J. (1995). Fuzzy-trace theory: an interim synthesis. *Learning and Individual
  Differences*, 7, 1–75.
- Richards, B. A., & Frankland, P. W. (2017). The persistence and transience of memory. *Neuron*, 94, 1071–1084.
- Roediger, H. L., & Karpicke, J. D. (2006). Test-enhanced learning: taking memory tests improves long-term
  retention. *Psychological Science*, 17, 249–255.
- Sinclair, A. H., & Barense, M. D. (2019). Prediction error and memory reactivation: how incomplete reminders drive
  reconsolidation. *Trends in Neurosciences*, 42, 727–739.
- Tambini, A., Ketz, N., & Davachi, L. (2010). Enhanced brain correlations during rest are related to memory for recent
  experiences. *Neuron*, 65, 280–290.
- Tononi, G., & Cirelli, C. (2014). Sleep and the price of plasticity. *Neuron*, 81, 12–34.
- Tse, D., Langston, R. F., Kakeyama, M., Bethus, I., Spooner, P. A., Wood, E. R., Witter, M. P., & Morris, R. G. M.
  (2007). Schemas and memory consolidation. *Science*, 316, 76–82.
- Underwood, B. J. (1957). Interference and forgetting. *Psychological Review*, 64, 49–60.
- Walker, M. P., Brakefield, T., Morgan, A., Hobson, J. A., & Stickgold, R. (2002). Practice with sleep makes perfect:
  sleep-dependent motor skill learning. *Neuron*, 35, 205–211.
- Wilson, M. A., & McNaughton, B. L. (1994). Reactivation of hippocampal ensemble memories during sleep. *Science*,
  265, 676–679.
- Wixted, J. T., & Ebbesen, E. B. (1991). On the form of forgetting. *Psychological Science*, 2, 409–415.
