# DECISIONS

## Things in the data that look odd, and what I did
1. **Dr. Rao's Monday windows overlap** (09:00-12:00, 11:45-15:00). Merged into one set of start times; plus a unique index as a backstop.
2. **Not every doctor has two windows a day.** I trust `clinic.json`, never assume.
3. **The paediatrician sees adults** (most of Dr. Sethi's bookings are adults). Speciality is therefore never a validity rule; a requested doctor is always honoured.
4. **`guardian_of` is one-directional** (Meera lists Kabir; Kabir lists nothing). Authority flows only guardian to ward.
5. **Shared phones are not identity or authority** (the Guptas, the Rawats). A phone narrows candidates; it never authorises acting on someone else's record.
6. **Near-identical names are different people** (Imran Qureshi / Imraan Quraishi). A phonetic key lets speech match despite spelling; by name alone they stay ambiguous and the phone separates them.
7. **`kal` means yesterday or tomorrow.** Chosen: tomorrow (appointments are future events). `kal` on 1 Oct is 2 Oct, a holiday, and the agent says so.
8. **A weekday said on that weekday** means next week; `aaj` means today.

## Design calls
- **Safety first, in code, on the raw turn.** Red flags (Hindi/English/Hinglish) fire before any parsing and about anyone ("pita behosh hain"). Negation suppresses a match only when adjacent ("seene mein dard nahi hai").
- **Writes are deferred until the last turn has cleared the gate.** Cost: a live chat would need an explicit confirm step; the batch-of-turns contract acts as that confirmation.
- **Escalate vs refuse vs abandon.** Escalate when a human must act (clinical, advice, authorisation, ambiguity, unknown patient). Refuse injection and bulk requests (nothing for a human to do). Abandon when nothing usable was said, the day is closed or full, the time is taken, or identity is incomplete.
- **Injected sentences are dropped, the rest of the turn is served**, and the reply says part of the message was ignored.
- **Identity.** Booking may proceed on a unique name match; cancel and reschedule additionally need a phone or DOB. Name and phone that point at different records means `ambiguous_patient`. Unknown name and phone means `out_of_scope` (no tool registers patients).
- **Time selection.** An explicit time must be free, otherwise the agent lists real free times and does not silently pick another. A window ("subah") takes the earliest free slot in it. No time at all means ask.
- **Corrections:** the last mention wins after a marker such as "nahi nahi"; "X ya Y" tries each in order; a weekday that disagrees with its date ("Mangalwar 7 tareekh") is asked about, not guessed.
- **Rescheduling to a different doctor** is not done silently: the agent asks, since that is really cancel plus book.

## Known weaknesses
- The language understanding is rules, exact on the 23 provided scripts but it will miss phrasings I did not anticipate (spoken digit phone numbers, Devanagari names, rare red-flag phrases). The bias is towards over-escalation, not unsafe bookings, but I have not proven that on unseen data.
- Past-tense symptoms ("seene mein dard tha") still escalate; a human can close them in seconds.
- The clock time of "today" is unknown, so "today 09:30" can be offered even if it has passed.
- No authentication on the operator UI or tool endpoints; this is a demo.
- No model is used. If you add one, it should only propose fields; the gate, validators and grounding guard must stay in front of it.
