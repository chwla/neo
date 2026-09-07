"""The questions a comparison asks, grouped by what the user is comparing models for.

Three constraints shaped every task in here, and they are worth stating because they rule
out most of what a benchmark would normally contain.

A right answer has to be *short*. The wall-clock cost of a comparison is almost entirely
generation, so a task whose correct answer is one word costs a second and a task that
wants an essay costs a minute. Every task below is written so that a good answer is small,
and carries a token cap that makes a rambling one cost something.

A right answer has to be *checkable without another model*. Anything graded by opinion
would need a judging pass, which is exactly the wait this feature is trying to avoid.

And a task has to *discriminate*. A question every model gets right measures nothing; so
does one they all fail. These lean on the places small local models actually come apart --
counting characters, following a stated format, arithmetic that invites the intuitive
wrong answer, writing code that parses.

There is a fourth rule that is about trust rather than speed, and it is the one that took
the most work: **a grader must accept every genuinely correct answer**. A check that only
passes one idiom stops measuring the model and starts measuring how closely it happened
to guess the author's phrasing. So the reversal task accepts a slice, ``reversed``, and
``.reverse()``; the division task accepts ``if b == 0`` and ``if b else``; the query task
names the tables rather than demanding the ``JOIN`` keyword. Where a task could not be
made unambiguous, it was rewritten rather than graded loosely -- the primary-colours
question became a formatting question about digits, because "primary colours" has two
correct answers depending on whether you mean light or paint.

Packs are ordered by ``rank``. A run takes the first N, so the lowest ranks are the most
representative of their use case rather than the easiest.
"""

from __future__ import annotations

import random
from dataclasses import replace

from app.services.model_compare import generators
from app.services.model_compare.generators import POOL_SIZE
from app.services.model_compare.grading import (
    AtMostWords,
    Contains,
    Equals,
    Excludes,
    IsJson,
    IsPython,
    LinesAtLeast,
    Matches,
    NumberIs,
    RegexAnswer,
    SentencesAtMost,
    Terse,
)
from app.services.model_compare.types import Task

# Kept short on purpose. A long preamble is tokens every model pays for on every task,
# and it also masks the differences in how well they follow the task's own instruction.
TERSE_SYSTEM = "Answer exactly what is asked, in the format asked for. Do not explain."
CODE_SYSTEM = "You are a careful programmer. Reply with code only, no commentary."


REASONING: tuple[Task, ...] = (
    Task(
        id="reasoning.bat_and_ball",
        use_case="reasoning",
        label="The bat and the ball",
        prompt=(
            "A bat and a ball cost $1.10 together. The bat costs $1.00 more than the ball. "
            "How much does the ball cost, in cents? Reply with only the number."
        ),
        system=TERSE_SYSTEM,
        max_tokens=400,
        rank=0,
        rubric="5 cents. $0.05 counts too. The intuitive answer, 10, is wrong.",
        checks=(
            # 0.05 is the same answer in dollars. Accepted because the model got the
            # arithmetic right and only read the unit differently.
            NumberIs(label="Got 5 cents", expected=5, accepts=(0.05,), weight=3.0),
            Terse(label="Answered with just the number", max_words=6),
        ),
    ),
    Task(
        id="reasoning.letter_count",
        use_case="reasoning",
        label="Counting letters",
        prompt=(
            "How many times does the letter r appear in the word strawberry? "
            "Reply with only the number."
        ),
        system=TERSE_SYSTEM,
        max_tokens=300,
        rank=1,
        rubric="3. Small models very often answer 2.",
        checks=(
            NumberIs(label="Got 3", expected=3, weight=3.0),
            Terse(label="Answered with just the number", max_words=6),
        ),
    ),
    Task(
        id="reasoning.sequence",
        use_case="reasoning",
        label="Next in the sequence",
        prompt="What number comes next: 2, 6, 12, 20, 30? Reply with only the number.",
        system=TERSE_SYSTEM,
        max_tokens=300,
        rank=2,
        rubric="42. The gaps grow by two each time: 4, 6, 8, 10, then 12.",
        checks=(
            NumberIs(label="Got 42", expected=42, weight=3.0),
            Terse(label="Answered with just the number", max_words=6),
        ),
    ),
    Task(
        id="reasoning.percentage",
        use_case="reasoning",
        label="Working backwards from a discount",
        prompt=(
            "A shirt costs $40 after a 20% discount. What was the price before the discount? "
            "Reply with only the number."
        ),
        system=TERSE_SYSTEM,
        max_tokens=400,
        rank=3,
        rubric="50. Dividing by 0.8, not adding 20% to 40, which gives the wrong 48.",
        checks=(
            NumberIs(label="Got 50", expected=50, weight=3.0),
            Terse(label="Answered with just the number", max_words=6),
        ),
    ),
    Task(
        id="reasoning.days",
        use_case="reasoning",
        label="Counting days forward",
        prompt=(
            "If today is Wednesday, what day of the week is it in 100 days? "
            "Reply with only the name of the day."
        ),
        system=TERSE_SYSTEM,
        max_tokens=400,
        rank=4,
        rubric="Friday. 100 divided by 7 leaves 2, so two days past Wednesday.",
        checks=(
            Equals(label="Got Friday", expected=("friday",), weight=3.0),
            Terse(label="Answered with just the day", max_words=4),
        ),
    ),
    Task(
        id="reasoning.speed",
        use_case="reasoning",
        label="Converting units",
        prompt=(
            "A car travels 60 km in 45 minutes. What is its average speed in km/h? "
            "Reply with only the number."
        ),
        system=TERSE_SYSTEM,
        max_tokens=350,
        rank=5,
        rubric="80. Three quarters of an hour, so 60 divided by 0.75.",
        checks=(
            NumberIs(label="Got 80", expected=80, weight=3.0),
            Terse(label="Answered with just the number", max_words=6),
        ),
    ),
    Task(
        id="reasoning.ordering",
        use_case="reasoning",
        label="Following a chain",
        prompt=(
            "Alice is taller than Bob. Bob is taller than Carol. Carol is taller than Dan. "
            "Who is the second tallest? Reply with only the name."
        ),
        system=TERSE_SYSTEM,
        max_tokens=300,
        rank=6,
        rubric="Bob. The order is Alice, Bob, Carol, Dan.",
        checks=(
            Equals(label="Got Bob", expected=("bob",), weight=3.0),
            Terse(label="Answered with just the name", max_words=4),
        ),
    ),
    Task(
        id="reasoning.syllogism",
        use_case="reasoning",
        label="A syllogism with nonsense words",
        prompt=(
            "All bloops are razzies. All razzies are lazzies. Are all bloops lazzies? "
            "Reply with only yes or no."
        ),
        system=TERSE_SYSTEM,
        max_tokens=250,
        rank=7,
        rubric="Yes. The nonsense words are there to stop it answering from memory.",
        checks=(
            Equals(label="Got yes", expected=("yes",), weight=3.0),
            Terse(label="Answered with one word", max_words=3),
        ),
    ),
)


CODING: tuple[Task, ...] = (
    Task(
        id="coding.function",
        use_case="coding",
        label="Writing a function",
        prompt=(
            "Write a Python function reverse_words(text) that returns the words of text in "
            "reverse order, separated by single spaces. Reply with only the code."
        ),
        system=CODE_SYSTEM,
        max_tokens=350,
        rank=0,
        rubric=(
            "Real Python defining reverse_words(text). Any way of reversing counts: a "
            "slice, reversed(), or list.reverse()."
        ),
        checks=(
            IsPython(label="Real Python that parses", function="reverse_words", arity=1,
                     weight=3.0),
            Contains(label="Splits the text into words", needles=("split",), weight=1.5),
            Contains(label="Joins them back together", needles=("join",), weight=1.5),
            # Every real idiom for reversing, not only the one that came to mind first.
            # A check that passes a slice but fails list.reverse() is measuring phrasing.
            Contains(
                label="Reverses them",
                needles=("[::-1]", "reversed", ".reverse(", "insert(0"),
                any_of=True,
                weight=2.0,
            ),
            Excludes(label="No commentary around it",
                     needles=("Here is", "Here's", "This function")),
        ),
    ),
    Task(
        id="coding.edge_case",
        use_case="coding",
        label="Handling an edge case",
        prompt=(
            "Write a Python function safe_divide(a, b) that returns a divided by b, "
            "or None when b is zero. Reply with only the code."
        ),
        system=CODE_SYSTEM,
        max_tokens=350,
        rank=1,
        rubric=(
            "Real Python defining safe_divide(a, b) that divides and returns None when b "
            "is zero. An explicit test, a truthiness test and try/except all count."
        ),
        checks=(
            IsPython(label="Real Python that parses", function="safe_divide", arity=2,
                     weight=3.0),
            # Returning None is the guard. Checking *how* it guarded fails `if b else
            # None`, which is both idiomatic and correct.
            Contains(label="Returns None for zero", needles=("None",), weight=2.0),
            Contains(label="Actually divides", needles=("/",), weight=1.0),
            Excludes(label="No commentary around it",
                     needles=("Here is", "Here's", "This function")),
        ),
    ),
    Task(
        id="coding.sql",
        use_case="coding",
        label="Writing a query",
        prompt=(
            "Tables: customers(id, name) and orders(id, customer_id, total). "
            "Write one SQL query returning the names of the 3 customers with the highest "
            "total order value. Reply with only the query."
        ),
        system=CODE_SYSTEM,
        max_tokens=300,
        rank=2,
        rubric=(
            "One query over both tables that sums each customer's orders, sorts highest "
            "first and returns three rows. An explicit JOIN and a comma join both count."
        ),
        checks=(
            # Naming both tables, rather than demanding the JOIN keyword: an implicit
            # comma join is valid SQL and answers the question.
            Contains(label="Uses both tables", needles=("customers", "orders"), weight=2.0),
            Contains(label="Totals each customer's orders", needles=("group by",), weight=2.0),
            Contains(label="Sorts highest first", needles=("order by",), weight=1.5),
            Contains(label="Sorts descending", needles=("desc",), weight=1.0),
            Contains(label="Returns three rows",
                     needles=("limit 3", "top 3", "top(3)", "fetch first 3"),
                     any_of=True, weight=1.5),
        ),
    ),
    Task(
        id="coding.regex",
        use_case="coding",
        label="Writing a pattern",
        prompt=(
            "Write a regular expression that matches a date in the form YYYY-MM-DD and "
            "nothing else. Reply with only the pattern, on one line."
        ),
        system=CODE_SYSTEM,
        max_tokens=250,
        rank=3,
        rubric=(
            "A pattern that matches 2026-09-06 and 1999-12-31 and rejects 2026-9-6, "
            "26-09-06 and plain text. Stricter month and day ranges are fine."
        ),
        checks=(
            RegexAnswer(
                label="Matches real dates, rejects wrong ones",
                should_match=("2026-09-06", "1999-12-31"),
                should_reject=("2026-9-6", "26-09-06", "not-a-date"),
                weight=3.0,
            ),
        ),
    ),
    Task(
        id="coding.complexity",
        use_case="coding",
        label="Knowing the cost of an algorithm",
        prompt=(
            "What is the worst-case time complexity of binary search on a sorted array? "
            "Reply with only the big-O notation."
        ),
        system=TERSE_SYSTEM,
        max_tokens=200,
        rank=4,
        rubric="O(log n), written any way round. O(n) and O(1) are wrong.",
        checks=(
            # A pattern rather than a needle: "log n" is a substring of "n log n", so a
            # Contains check would give merge sort full marks for binary search.
            Matches(
                label="Got O(log n)",
                pattern=r"o\s*\(?\s*log",
                detail="Expected O(log n).",
                weight=3.0,
            ),
            Terse(label="Answered with just the notation", max_words=8),
        ),
    ),
    Task(
        id="coding.git",
        use_case="coding",
        label="Recalling a command",
        prompt=(
            "Which single git command undoes the most recent commit while keeping its "
            "changes staged? Reply with only the command."
        ),
        system=TERSE_SYSTEM,
        max_tokens=200,
        rank=5,
        rubric="git reset --soft HEAD~1. HEAD^ and HEAD~ are the same reference.",
        checks=(
            Contains(label="Got git reset --soft", needles=("reset --soft",), weight=3.0),
            Contains(label="Points at the last commit", needles=("head~1", "head^", "head~"),
                     any_of=True),
            Terse(label="Answered with just the command", max_words=10),
        ),
    ),
    Task(
        id="coding.bug",
        use_case="coding",
        label="Spotting a bug",
        prompt=(
            "This Python is meant to return the largest number in a list but is wrong:\n"
            "def largest(items):\n"
            "    best = 0\n"
            "    for item in items:\n"
            "        if item > best:\n"
            "            best = item\n"
            "    return best\n"
            "In one sentence, what is the bug? Reply with only that sentence."
        ),
        system=TERSE_SYSTEM,
        max_tokens=250,
        rank=6,
        rubric=(
            "best starts at 0, so an all-negative list returns 0. Naming the faulty "
            "starting value and naming the all-negative case are the same finding."
        ),
        checks=(
            # Two ways of saying the same thing: the symptom (all-negative lists) and the
            # cause (the starting value). Both are the right answer.
            Contains(
                label="Names the faulty starting value or its effect",
                needles=(
                    "negative", "below zero", "less than zero",
                    "initial", "initialis", "initializ", "starts at 0", "starting at 0",
                    "first element", "-inf", "empty",
                ),
                any_of=True,
                weight=3.0,
            ),
            SentencesAtMost(label="Kept it to one sentence", limit=1),
        ),
    ),
    Task(
        id="coding.docstring",
        use_case="coding",
        label="Reading unfamiliar code",
        prompt=(
            "What does this Python return for the input [3, 1, 2]?\n"
            "def f(xs):\n"
            "    return sorted(xs)[len(xs) // 2]\n"
            "Reply with only the value."
        ),
        system=TERSE_SYSTEM,
        max_tokens=250,
        rank=7,
        rubric="2. Sorted gives [1, 2, 3]; index 3 // 2 is 1; that element is 2.",
        checks=(
            NumberIs(label="Got 2", expected=2, weight=3.0),
            Terse(label="Answered with just the value", max_words=6),
        ),
    ),
)


STRUCTURED: tuple[Task, ...] = (
    Task(
        id="structured.json_extract",
        use_case="structured",
        label="Pulling fields out of a sentence",
        prompt=(
            'From this sentence, return a JSON object with the keys name, city and age: '
            '"Priya Raman is 34 and lives in Chennai." Reply with only the JSON.'
        ),
        system=TERSE_SYSTEM,
        max_tokens=250,
        rank=0,
        rubric=(
            'Valid JSON with name, city and age. City must be Chennai and age 34; the age '
            "may be a number or a string."
        ),
        checks=(
            IsJson(
                label="Valid JSON with the right values",
                keys=("name", "city", "age"),
                values=(("city", "Chennai"), ("age", "34")),
                weight=3.0,
            ),
        ),
    ),
    Task(
        id="structured.exact_word",
        use_case="structured",
        label="Doing exactly as told",
        prompt="Reply with exactly the word BANANA and nothing else.",
        system=TERSE_SYSTEM,
        max_tokens=100,
        rank=1,
        rubric="The single word BANANA, with nothing around it.",
        checks=(
            Equals(label="Said BANANA", expected=("banana",), weight=2.0),
            Terse(label="Said nothing else", max_words=1, weight=2.0),
        ),
    ),
    Task(
        id="structured.csv",
        use_case="structured",
        label="Producing a table",
        prompt=(
            "Return these as CSV with a header row of item,price and no other text: "
            "a pen costs 2, a book costs 15, a lamp costs 40."
        ),
        system=TERSE_SYSTEM,
        max_tokens=200,
        rank=2,
        rubric="A header row then three rows, one per item. A space after the comma is fine.",
        checks=(
            Contains(label="Has the header asked for", needles=("item,price", "item, price"),
                     any_of=True, weight=2.0),
            LinesAtLeast(label="One row per item", minimum=4, weight=2.0),
            Contains(label="Carries every item", needles=("pen", "book", "lamp"), weight=1.5),
            Excludes(label="No commentary around it", needles=("here", "csv:", "```")),
        ),
    ),
    Task(
        id="structured.list",
        use_case="structured",
        label="Keeping to a list",
        prompt=(
            "List exactly three uses for a paperclip, as a numbered list. "
            "No introduction, no conclusion."
        ),
        system=TERSE_SYSTEM,
        max_tokens=200,
        rank=3,
        rubric="Three numbered lines and nothing else. What the uses are does not matter.",
        checks=(
            LinesAtLeast(label="Three numbered items", minimum=3, numbered=True, weight=2.0),
            AtMostWords(label="Stayed brief", limit=40),
            Excludes(label="No introduction", needles=("here are", "sure,", "certainly")),
        ),
    ),
    Task(
        id="structured.json_nested",
        use_case="structured",
        label="Nesting a structure",
        prompt=(
            'Return JSON with a key "order" whose value is an object with keys id and items, '
            'where id is 7 and items is the list ["pen", "book"]. Reply with only the JSON.'
        ),
        system=TERSE_SYSTEM,
        max_tokens=250,
        rank=4,
        rubric='Valid JSON shaped {"order": {"id": 7, "items": ["pen", "book"]}}.',
        checks=(
            IsJson(label="Valid JSON with the right shape", keys=("order",), weight=2.0),
            Contains(label="Carries the values asked for", needles=("pen", "book", "7"),
                     weight=2.0),
        ),
    ),
    Task(
        id="structured.no_markdown",
        use_case="structured",
        label="Withholding formatting",
        prompt=(
            "Write the numbers one to five as digits, separated by commas, on one line. "
            "Use no markdown, no bullet points and no other text."
        ),
        system=TERSE_SYSTEM,
        max_tokens=150,
        rank=5,
        # This task used to ask for the three primary colours, which has two correct
        # answers depending on whether you mean light or paint -- so it was rewritten
        # rather than graded loosely. The task was never about the trivia.
        rubric="1,2,3,4,5 on one line, with no markdown and nothing else.",
        checks=(
            Contains(label="Has all five numbers", needles=("1", "2", "3", "4", "5"),
                     weight=2.0),
            Excludes(label="No markdown", needles=("*", "#", "- ", "```")),
            Terse(label="Kept to one line", max_words=8),
        ),
    ),
)


WRITING: tuple[Task, ...] = (
    Task(
        id="writing.summarise",
        use_case="writing",
        label="Summarising to a length",
        prompt=(
            "Summarise this in exactly one sentence of at most 20 words:\n"
            "The city council voted on Tuesday to extend the tram line by four stops, "
            "adding service to the eastern suburbs by 2029. The extension will cost an "
            "estimated 240 million and was opposed by three councillors who argued the "
            "money would be better spent on bus frequency."
        ),
        system=TERSE_SYSTEM,
        max_tokens=200,
        rank=0,
        rubric="One sentence, 20 words or fewer, that keeps the tram extension as the point.",
        checks=(
            AtMostWords(label="Stayed within 20 words", limit=20, weight=2.0),
            SentencesAtMost(label="Kept it to one sentence", limit=1, weight=2.0),
            Contains(label="Kept the point", needles=("tram", "line", "extension"), any_of=True,
                     weight=2.0),
        ),
    ),
    Task(
        id="writing.tone",
        use_case="writing",
        label="Changing the tone",
        prompt=(
            "Rewrite this politely, in one sentence: "
            '"Your report is a mess and you clearly did not bother to check it." '
            "Reply with only the rewrite."
        ),
        system=TERSE_SYSTEM,
        max_tokens=200,
        rank=1,
        rubric=(
            "One polite sentence that drops the insult but still asks for the report to "
            "be checked."
        ),
        checks=(
            # "clearly" was once on this list and is not any more: a polite rewrite may
            # legitimately ask for something to be made clearer.
            Excludes(label="Dropped the rudeness",
                     needles=("mess", "did not bother", "didn't bother", "sloppy", "careless"),
                     weight=3.0),
            Contains(label="Sounds polite",
                     needles=("please", "could", "would", "thank", "appreciate", "perhaps",
                              "might", "kindly"),
                     any_of=True, weight=1.5),
            SentencesAtMost(label="Kept it to one sentence", limit=1),
            AtMostWords(label="Stayed brief", limit=45),
        ),
    ),
    Task(
        id="writing.subject",
        use_case="writing",
        label="Writing a subject line",
        prompt=(
            "Write an email subject line, at most 8 words, for a message telling a team "
            "that Friday's release is delayed to Monday. Reply with only the subject line."
        ),
        system=TERSE_SYSTEM,
        max_tokens=120,
        rank=2,
        rubric="Eight words or fewer, saying the release has moved.",
        checks=(
            AtMostWords(label="At most 8 words", limit=8, weight=2.0),
            Contains(label="Says what changed",
                     needles=("delay", "postpon", "moved", "monday", "slip", "push"),
                     any_of=True, weight=2.0),
        ),
    ),
    Task(
        id="writing.shorten",
        use_case="writing",
        label="Cutting a sentence down",
        prompt=(
            "Rewrite this in at most 10 words without losing the meaning: "
            '"Due to the fact that it was raining very heavily, we made the decision to '
            'postpone the event until a later date." Reply with only the rewrite.'
        ),
        system=TERSE_SYSTEM,
        max_tokens=150,
        rank=3,
        rubric="Ten words or fewer, keeping both the rain and the postponement.",
        checks=(
            AtMostWords(label="At most 10 words", limit=10, weight=3.0),
            Contains(label="Kept the rain", needles=("rain",), weight=1.5),
            Contains(label="Kept the postponement",
                     needles=("postpon", "delay", "moved", "reschedul", "put off", "later"),
                     any_of=True, weight=1.5),
        ),
    ),
    Task(
        id="writing.plain",
        use_case="writing",
        label="Removing jargon",
        prompt=(
            "Rewrite this in plain English, one sentence: "
            '"We will leverage synergies to operationalise a best-in-class paradigm." '
            "Reply with only the rewrite."
        ),
        system=TERSE_SYSTEM,
        max_tokens=150,
        rank=4,
        rubric="One sentence with none of the jargon words left in it.",
        checks=(
            Excludes(label="Jargon gone", needles=("leverage", "synergy", "synergies",
                                                   "operationalise", "operationalize",
                                                   "paradigm", "best-in-class"),
                     weight=3.0),
            SentencesAtMost(label="Kept it to one sentence", limit=1),
        ),
    ),
    Task(
        id="writing.headline",
        use_case="writing",
        label="Writing to a constraint",
        prompt=(
            "Write a six-word headline for a story about a dog that learned to ride a bus "
            "to the park alone. Reply with only the headline."
        ),
        system=TERSE_SYSTEM,
        max_tokens=120,
        rank=5,
        rubric="Six words or fewer, and about the dog or the bus.",
        checks=(
            AtMostWords(label="Six words or fewer", limit=6, weight=3.0),
            Contains(label="About the story", needles=("dog", "bus"), any_of=True, weight=2.0),
        ),
    ),
)


CHAT: tuple[Task, ...] = (
    Task(
        id="chat.capital",
        use_case="chat",
        label="A fact people get wrong",
        prompt="What is the capital city of Australia? Reply with only the city name.",
        system=TERSE_SYSTEM,
        max_tokens=150,
        rank=0,
        rubric="Canberra. Sydney and Melbourne are the common wrong answers.",
        checks=(
            Equals(label="Got Canberra", expected=("canberra",), weight=3.0),
            Terse(label="Answered with just the city", max_words=4),
        ),
    ),
    Task(
        id="chat.conversion",
        use_case="chat",
        label="An everyday conversion",
        prompt=(
            "Convert 100 degrees Fahrenheit to Celsius, rounded to one decimal place. "
            "Reply with only the number."
        ),
        system=TERSE_SYSTEM,
        max_tokens=300,
        rank=1,
        rubric="37.8. Anything between 37.65 and 37.95 counts as the same answer.",
        checks=(
            NumberIs(label="Got 37.8", expected=37.8, tolerance=0.15, weight=3.0),
            Terse(label="Answered with just the number", max_words=6),
        ),
    ),
    Task(
        id="chat.explain",
        use_case="chat",
        label="Explaining something simply",
        prompt=(
            "Explain what a database index is to someone who has never programmed, "
            "in at most 30 words. Reply with only the explanation."
        ),
        system=TERSE_SYSTEM,
        max_tokens=200,
        rank=2,
        rubric=(
            "Thirty words or fewer, reaching for the idea of finding things faster, and "
            "without falling back on jargon."
        ),
        checks=(
            AtMostWords(label="Stayed within 30 words", limit=30, weight=2.0),
            Contains(label="Reached for the right idea",
                     needles=("look", "find", "faster", "quick", "book", "search", "index card"),
                     any_of=True, weight=2.0),
            Excludes(label="No jargon left in", needles=("b-tree", "btree", "cardinality",
                                                         "query planner")),
        ),
    ),
    Task(
        id="chat.year",
        use_case="chat",
        label="A date from memory",
        prompt="In what year did the Berlin Wall fall? Reply with only the year.",
        system=TERSE_SYSTEM,
        max_tokens=150,
        rank=3,
        rubric="1989.",
        checks=(
            NumberIs(label="Got 1989", expected=1989, weight=3.0),
            Terse(label="Answered with just the year", max_words=4),
        ),
    ),
    Task(
        id="chat.unanswerable",
        use_case="chat",
        label="Admitting it does not know",
        prompt=(
            "What did I have for breakfast this morning? "
            "If you cannot know, say exactly: I don't know."
        ),
        system=TERSE_SYSTEM,
        max_tokens=150,
        rank=4,
        rubric=(
            "Any clear admission that it cannot know. The prompt asks for one phrasing, "
            "but every honest refusal counts -- what is being tested is that it does not "
            "invent a breakfast."
        ),
        checks=(
            # Broad on purpose. "I have no way of knowing" is the same finding as "I
            # don't know"; grading only the literal phrase would measure obedience rather
            # than honesty, and honesty is the interesting property here.
            Contains(
                label="Admitted it cannot know",
                needles=(
                    "i don't know", "i dont know", "i do not know",
                    "cannot know", "can't know", "no way to know", "no way of knowing",
                    "don't have access", "do not have access", "unable to know",
                    "cannot tell", "can't tell", "i have no idea",
                ),
                any_of=True,
                weight=3.0,
            ),
            Terse(label="Did not pad the answer", max_words=20),
        ),
    ),
    Task(
        id="chat.followup",
        use_case="chat",
        label="Holding two constraints at once",
        prompt=(
            "Name a fruit that is red and grows on a tree. "
            "Reply with only the fruit, one word."
        ),
        system=TERSE_SYSTEM,
        max_tokens=150,
        rank=5,
        rubric=(
            "Any red fruit that grows on a tree: apple, cherry, plum, peach, pomegranate, "
            "lychee and others all count. Strawberries do not -- they do not grow on trees."
        ),
        checks=(
            Equals(
                label="Named a red tree fruit",
                expected=(
                    "apple", "cherry", "cherries", "pomegranate", "plum", "peach",
                    "nectarine", "lychee", "rambutan", "mulberry", "persimmon",
                    "crabapple", "guava", "apricot",
                ),
                weight=3.0,
            ),
            Terse(label="Answered with one word", max_words=3),
        ),
    ),
)


#: The canonical answer for each hand-written question, applied below. Kept here rather
#: than inline so the questions above stay readable, and so the suite can assert that
#: every question in every pack accepts its own answer.
_CANONICAL: dict[str, str] = {
    "reasoning.bat_and_ball": "5",
    "reasoning.letter_count": "3",
    "reasoning.sequence": "42",
    "reasoning.percentage": "50",
    "reasoning.days": "Friday",
    "reasoning.speed": "80",
    "reasoning.ordering": "Bob",
    "reasoning.syllogism": "Yes",
    "coding.function": "def reverse_words(text):\n    return ' '.join(text.split()[::-1])",
    "coding.edge_case": (
        "def safe_divide(a, b):\n    if b == 0:\n        return None\n    return a / b"
    ),
    "coding.sql": (
        "SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.id "
        "GROUP BY c.name ORDER BY SUM(o.total) DESC LIMIT 3"
    ),
    "coding.regex": r"\d{4}-\d{2}-\d{2}",
    "coding.complexity": "O(log n)",
    "coding.git": "git reset --soft HEAD~1",
    "coding.bug": "It returns 0 when every number in the list is negative.",
    "coding.docstring": "2",
    "structured.json_extract": '{"name": "Priya Raman", "city": "Chennai", "age": 34}',
    "structured.exact_word": "BANANA",
    "structured.csv": "item,price\npen,2\nbook,15\nlamp,40",
    "structured.list": "1. Reset a SIM tray\n2. Hold papers together\n3. Hang a picture",
    "structured.json_nested": '{"order": {"id": 7, "items": ["pen", "book"]}}',
    "structured.no_markdown": "1,2,3,4,5",
    "writing.summarise": "The council voted to extend the tram line by four stops by 2029.",
    "writing.tone": "Could you please take another look at the report before we send it?",
    "writing.subject": "Release moved from Friday to Monday",
    "writing.shorten": "Heavy rain forced us to postpone the event.",
    "writing.plain": "We will work together to build something excellent.",
    "writing.headline": "Dog rides bus to park alone",
    "chat.capital": "Canberra",
    "chat.conversion": "37.8",
    "chat.explain": (
        "It is like the contents page of a book: it lets the computer find the rows you "
        "asked for without reading every page."
    ),
    "chat.year": "1989",
    "chat.unanswerable": "I don't know.",
    "chat.followup": "Apple",
}

#: The hand-written questions, which lead every pack. They are the most carefully tuned
#: -- each was chosen because it separates models rather than because it was easy to
#: grade -- so they come first and the generated pool fills in behind them.
_CORE: dict[str, tuple[Task, ...]] = {
    "chat": CHAT,
    "writing": WRITING,
    "coding": CODING,
    "reasoning": REASONING,
    "structured": STRUCTURED,
}


def _assemble(use_case: str) -> tuple[Task, ...]:
    """One pack of exactly ``POOL_SIZE`` questions, in a fixed order.

    Fixed because a question's id is how a stored result says what was asked; if the
    catalogue shuffled between restarts, an id would stop meaning anything. The variety
    comes from a *run* sampling this pool, not from the pool moving underneath it.
    """

    # Deduplicated on the prompt as well as the id. Several hand-written questions are
    # also the first entry of their generated family -- the same question under two ids --
    # and a run that sampled both would ask it twice while reporting a hundred distinct
    # questions. The hand-written one comes first and wins.
    seen: set[str] = set()
    asked: set[str] = set()
    pool: list[Task] = []
    for task in (*_CORE.get(use_case, ()), *generators.build(use_case)):
        if task.id in seen or task.prompt in asked:
            continue
        seen.add(task.id)
        asked.add(task.prompt)
        pool.append(
            task if task.canonical else replace(task, canonical=_CANONICAL.get(task.id, ""))
        )
        if len(pool) == POOL_SIZE:
            break
    return tuple(pool)


PACKS: dict[str, tuple[Task, ...]] = {
    name: _assemble(name)
    for name in ("chat", "writing", "coding", "reasoning", "structured")
}

# How each choice is worded on screen, in the register the local-models wizard uses:
# things a person wants to do, not capabilities a model has. Deliberately framed as
# workloads rather than as a search for the cleverest model -- which one suits the work
# is the question this feature can actually answer.
USE_CASE_CHOICES: list[dict[str, str]] = [
    {
        "id": "coding",
        "label": "Programming",
        "detail": "Writing code, queries and patterns that actually work.",
    },
    {
        "id": "reasoning",
        "label": "Working things out",
        "detail": "Puzzles and arithmetic where the obvious answer is wrong.",
    },
    {
        "id": "writing",
        "label": "Writing and editing",
        "detail": "Rewriting, shortening and changing tone to order.",
    },
    {
        "id": "chat",
        "label": "General questions",
        "detail": "Everyday facts, explanations and knowing when to say no.",
    },
    {
        "id": "structured",
        "label": "Following a format",
        "detail": "Returning JSON, lists and tables exactly as asked.",
    },
    {
        "id": "custom",
        "label": "Something of your own",
        "detail": "Your own questions, side by side. You judge, or a model can.",
    },
]

#: How many questions of your own a comparison may carry. The same ceiling the built-in
#: packs are held to, so neither mode is arbitrarily the more limited one.
MAX_CUSTOM_PROMPTS = POOL_SIZE

#: Roomier than the built-ins: a question of your own has no known answer length, and
#: cutting it off mid-sentence would make every model look equally bad.
CUSTOM_MAX_TOKENS = 600


def pack(use_case: str) -> tuple[Task, ...]:
    """Every task for a use case, most representative first."""

    return tuple(sorted(PACKS.get(use_case, ()), key=lambda task: task.rank))


#: The most questions one run will ask, which is also how many each pack holds. There is
#: no need for a separate ceiling: a run can ask for the whole pack and no more.
MAX_DEPTH = POOL_SIZE


def select(use_case: str, depth: int, seed: int | None = None) -> list[Task]:
    """``depth`` questions drawn at random from the pack of a hundred.

    Random rather than the first few, because a fixed prefix means every run of a use
    case asks the same handful: a model that happens to be good at those looks better
    than it is, and running the comparison again tells you nothing new. Sampling covers
    different ground each time.

    Random sampling would ordinarily cost reproducibility, which a measurement tool
    cannot afford -- two runs whose questions differ are not comparable, and a surprising
    result has to be something the user can go back and check. So the draw is seeded, the
    seed is recorded on the run, and passing it back reproduces the exact set. Without
    one, a fresh seed is chosen and written down.

    The sample is returned in pool order rather than draw order, so the grid reads the
    same way whichever questions came up.
    """

    pool = list(pack(use_case))
    if not pool:
        return []
    wanted = max(1, min(depth, len(pool)))
    picked = random.Random(seed).sample(range(len(pool)), wanted)
    return [pool[index] for index in sorted(picked)]


def new_seed() -> int:
    """A seed for a run that did not bring one. Recorded so the draw can be repeated."""

    return random.randrange(2**32)


def custom_tasks(prompts: list[str] | str) -> list[Task]:
    """The user's own questions, one task each.

    Carries no checks, and that is the point rather than an omission: there is no rule
    that grades an arbitrary question, and inventing one would put a number on screen
    that means nothing. These tasks report as "not evaluated" unless a judging pass is
    asked for.
    """

    if isinstance(prompts, str):
        prompts = [prompts]
    cleaned = [item.strip() for item in prompts if item and item.strip()]
    return [
        Task(
            id=f"custom.{index + 1}",
            use_case="custom",
            label=f"Question {index + 1}" if len(cleaned) > 1 else "Your question",
            prompt=prompt,
            system="",
            max_tokens=CUSTOM_MAX_TOKENS,
            rank=index,
            checks=(),
            rubric="Your own question. There is no rule that can check this automatically.",
        )
        for index, prompt in enumerate(cleaned)
    ]


def total_tasks(use_case: str) -> int:
    """How many different questions a pack holds. A run samples from these."""

    return len(pack(use_case))
