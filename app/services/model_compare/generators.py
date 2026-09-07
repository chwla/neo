"""Building the question pools, a hundred to a pack.

A comparison is only as good as the questions behind it, and a pack of eight has two
problems: a run can only ever ask those eight, and a model that happens to be good at
one of them moves the score a lot. A hundred fixes both -- a run samples from them, so
two runs cover different ground and no single question dominates.

Writing five hundred questions by hand would produce five hundred chances to write the
answer key down wrong, so almost nothing here is hand-written. Each family is a shape
with a table of parameters, and **the expected answer is computed from those parameters
rather than typed**: the letter-counting questions call ``str.count``, the arithmetic
ones do the arithmetic, the code-tracing ones evaluate the same expression the question
asks about. A wrong answer key is not merely unlikely, it is unrepresentable.

Every task carries its own ``canonical`` answer, and the suite asserts that all of them
score full marks. That is what makes a pool this size safe to change.

The catalogue itself is fixed: same hundred questions, in the same order, every time the
process starts. The randomness in this feature is which of them a *run* samples, and
that is decided in ``tasks.py`` against a seed the run records.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

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

TERSE_SYSTEM = "Answer exactly what is asked, in the format asked for. Do not explain."
CODE_SYSTEM = "You are a careful programmer. Reply with code only, no commentary."

#: How many questions each pack ends up holding.
POOL_SIZE = 100


def _number_task(
    *,
    id: str,
    use_case: str,
    label: str,
    prompt: str,
    answer: float,
    rubric: str,
    max_tokens: int = 350,
    tolerance: float = 0.001,
    accepts: tuple[float, ...] = (),
    terse_words: int = 6,
) -> Task:
    """A question whose answer is a number, graded on the number and on brevity."""

    return Task(
        id=id,
        use_case=use_case,
        label=label,
        prompt=f"{prompt} Reply with only the number.",
        system=TERSE_SYSTEM,
        max_tokens=max_tokens,
        rubric=rubric,
        canonical=f"{answer:g}",
        checks=(
            NumberIs(label="Right number", expected=answer, tolerance=tolerance,
                     accepts=accepts, weight=3.0),
            Terse(label="Answered with just the number", max_words=terse_words),
        ),
    )


def _word_task(
    *,
    id: str,
    use_case: str,
    label: str,
    prompt: str,
    expected: tuple[str, ...],
    rubric: str,
    canonical: str = "",
    max_tokens: int = 200,
    terse_words: int = 4,
) -> Task:
    """A question whose answer is a word, matched on whole words anywhere in the reply."""

    return Task(
        id=id,
        use_case=use_case,
        label=label,
        prompt=prompt,
        system=TERSE_SYSTEM,
        max_tokens=max_tokens,
        rubric=rubric,
        canonical=canonical or expected[0],
        checks=(
            Equals(label="Right answer", expected=expected, weight=3.0),
            Terse(label="Answered in a word", max_words=terse_words),
        ),
    )


# --------------------------------------------------------------------------- reasoning

#: Words with an interesting number of one letter. The count is never written down here;
#: it is read off the word, so it cannot disagree with the word.
_COUNTING = [
    ("strawberry", "r"), ("bookkeeper", "e"), ("mississippi", "s"), ("banana", "a"),
    ("possessions", "s"), ("committee", "m"), ("accommodation", "c"), ("beekeeper", "e"),
    ("assassination", "s"), ("parallel", "l"), ("millennium", "n"), ("necessary", "s"),
    ("occurrence", "r"), ("embarrassment", "r"), ("bittersweet", "t"), ("aardvark", "a"),
    ("rhythm", "h"), ("sheepish", "h"), ("nonsense", "n"), ("tattletale", "t"),
]

#: (start day, days ahead). The answer is computed, not stored.
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

#: (name of thing, total in pounds, how much more the dearer one costs)
_BAT_AND_BALL = [
    ("bat", "ball", 110, 100), ("laptop", "case", 220, 200), ("desk", "lamp", 130, 120),
    ("coat", "scarf", 150, 140), ("phone", "charger", 310, 300), ("bike", "helmet", 260, 250),
    ("guitar", "strap", 190, 180), ("camera", "strap", 170, 160),
]

#: (people, tallest-first) for the ordering questions.
_CHAINS = [
    ("Alice", "Bob", "Carol", "Dan"), ("Erin", "Frank", "Gita", "Hugo"),
    ("Ivan", "Jo", "Kim", "Leo"), ("Mira", "Nils", "Omar", "Pia"),
    ("Quinn", "Rosa", "Sam", "Tara"), ("Uma", "Viktor", "Wren", "Xiu"),
]

_NONSENSE = [
    ("bloops", "razzies", "lazzies"), ("wugs", "flimps", "dorbs"),
    ("kribs", "snerts", "plonks"), ("vims", "quoles", "harns"),
    ("zorbs", "mekkins", "trellis"), ("plips", "grunes", "sarbs"),
]


def _reasoning() -> Iterator[Task]:
    # Counting a letter, which small models get wrong far more often than they should.
    for index, (word, letter) in enumerate(_COUNTING):
        yield _number_task(
            id=f"reasoning.count.{index}",
            use_case="reasoning",
            label=f"Counting letters in {word}",
            prompt=f"How many times does the letter {letter} appear in the word {word}?",
            answer=word.count(letter),
            rubric=f"{word.count(letter)}. Counted straight off the word.",
            max_tokens=300,
        )

    # The bat-and-ball trap, where the intuitive answer is always wrong.
    for index, (dear, cheap, total, difference) in enumerate(_BAT_AND_BALL):
        cheap_cents = (total - difference) // 2
        yield _number_task(
            id=f"reasoning.trap.{index}",
            use_case="reasoning",
            label=f"The {dear} and the {cheap}",
            prompt=(
                f"A {dear} and a {cheap} cost ${total / 100:.2f} together. The {dear} costs "
                f"${difference / 100:.2f} more than the {cheap}. How much does the {cheap} "
                "cost, in cents?"
            ),
            answer=cheap_cents,
            accepts=(cheap_cents / 100,),
            rubric=(
                f"{cheap_cents} cents. The intuitive answer, {total - difference}, is wrong."
            ),
            max_tokens=400,
        )

    # Days of the week, where the arithmetic is a remainder in disguise.
    for index, (start, ahead) in enumerate(
        [(d, n) for d in _WEEKDAYS for n in (100, 50, 30, 365)][:20]
    ):
        landing = _WEEKDAYS[(_WEEKDAYS.index(start) + ahead) % 7]
        yield _word_task(
            id=f"reasoning.days.{index}",
            use_case="reasoning",
            label=f"{ahead} days after {start}",
            prompt=(
                f"If today is {start}, what day of the week is it in {ahead} days? "
                "Reply with only the name of the day."
            ),
            expected=(landing.lower(),),
            rubric=f"{landing}. {ahead} divided by 7 leaves {ahead % 7}.",
            max_tokens=400,
        )

    # Working back through a discount, where adding the percentage back is the trap.
    for index, (after, percent) in enumerate(
        [(40, 20), (75, 25), (90, 10), (60, 40), (120, 20), (45, 50),
         (84, 30), (99, 10), (65, 35), (150, 25)]
    ):
        before = after / (1 - percent / 100)
        yield _number_task(
            id=f"reasoning.discount.{index}",
            use_case="reasoning",
            label=f"Before a {percent}% discount",
            prompt=(
                f"A shirt costs ${after:g} after a {percent}% discount. What was the price "
                "before the discount?"
            ),
            answer=round(before, 2),
            tolerance=0.05,
            rubric=f"{before:g}. Divide by {1 - percent / 100:g}, do not add {percent}% back.",
            max_tokens=400,
        )

    # Speed, which is a unit conversion wearing a word problem.
    for index, (km, minutes) in enumerate(
        [(60, 45), (30, 20), (100, 75), (45, 30), (18, 12), (90, 40),
         (25, 50), (120, 90), (14, 21), (75, 25)]
    ):
        yield _number_task(
            id=f"reasoning.speed.{index}",
            use_case="reasoning",
            label=f"{km} km in {minutes} minutes",
            prompt=(
                f"A car travels {km} km in {minutes} minutes. What is its average speed "
                "in km/h?"
            ),
            answer=round(km / (minutes / 60), 2),
            tolerance=0.05,
            rubric=f"{km / (minutes / 60):g} km/h.",
        )

    # Following a chain of comparisons to a particular position.
    for index, chain in enumerate(_CHAINS):
        for place, ordinal in ((1, "second"), (2, "third")):
            yield _word_task(
                id=f"reasoning.order.{index}.{place}",
                use_case="reasoning",
                label=f"{ordinal.title()} tallest of four",
                prompt=(
                    f"{chain[0]} is taller than {chain[1]}. {chain[1]} is taller than "
                    f"{chain[2]}. {chain[2]} is taller than {chain[3]}. Who is the "
                    f"{ordinal} tallest? Reply with only the name."
                ),
                expected=(chain[place].lower(),),
                rubric=f"{chain[place]}. The order is {', '.join(chain)}.",
                max_tokens=300,
            )

    # A syllogism in words the model cannot have memorised.
    for index, (a, b, c) in enumerate(_NONSENSE):
        yield _word_task(
            id=f"reasoning.syllogism.{index}",
            use_case="reasoning",
            label=f"All {a} are {c}?",
            prompt=(
                f"All {a} are {b}. All {b} are {c}. Are all {a} {c}? "
                "Reply with only yes or no."
            ),
            expected=("yes",),
            rubric="Yes. The nonsense words stop it answering from memory.",
            max_tokens=250,
            terse_words=3,
        )

    # Ages, remainders, averages and rates: ordinary arithmetic with an exact answer.
    for index, (times, total) in enumerate([(2, 30), (3, 40), (4, 25), (2, 51), (5, 42)]):
        younger = total / (times + 1)
        yield _number_task(
            id=f"reasoning.age.{index}",
            use_case="reasoning",
            label="Ages that sum to a total",
            prompt=(
                f"Ana is {times} times as old as Ben. Together they are {total}. "
                "How old is Ben?"
            ),
            answer=round(younger, 2),
            tolerance=0.05,
            rubric=f"{younger:g}. Divide {total} by {times + 1}.",
            max_tokens=400,
        )

    for index, (value, divisor) in enumerate(
        [(137, 8), (250, 7), (1000, 3), (81, 5), (444, 9), (99, 4)]
    ):
        yield _number_task(
            id=f"reasoning.remainder.{index}",
            use_case="reasoning",
            label=f"Remainder of {value} over {divisor}",
            prompt=f"What is the remainder when {value} is divided by {divisor}?",
            answer=value % divisor,
            rubric=f"{value % divisor}.",
            max_tokens=300,
        )

    for index, (numbers, target) in enumerate(
        [((4, 8, 9), 8), ((10, 20, 30, 40), 30), ((3, 5, 7, 9, 11), 9),
         ((12, 15), 20), ((2, 4, 6, 8), 7)]
    ):
        # The value the last number must take for the mean to be `target`.
        missing = target * (len(numbers) + 1) - sum(numbers)
        yield _number_task(
            id=f"reasoning.average.{index}",
            use_case="reasoning",
            label="Finding the missing number",
            prompt=(
                f"The average of {', '.join(str(n) for n in numbers)} and one more number "
                f"is {target}. What is that number?"
            ),
            answer=missing,
            rubric=f"{missing}.",
            max_tokens=400,
        )

    # The machines-and-widgets trap: the rate per machine does not change.
    for index, (n, minutes) in enumerate([(5, 5), (3, 3), (7, 7), (4, 4), (6, 6)]):
        yield _number_task(
            id=f"reasoning.rate.{index}",
            use_case="reasoning",
            label="Machines and widgets",
            prompt=(
                f"If {n} machines make {n} widgets in {minutes} minutes, how many minutes "
                f"do 100 machines take to make 100 widgets?"
            ),
            answer=minutes,
            rubric=f"{minutes}. Each machine's rate is unchanged; {n * 20} is the trap.",
            max_tokens=400,
        )


# ----------------------------------------------------------------------------- coding

#: (expression over xs, a concrete input) -- the answer is obtained by running the same
#: expression here, so the question and its answer key cannot drift apart.
_TRACES = [
    ("sorted(xs)[len(xs) // 2]", [3, 1, 2]), ("sum(xs[1:])", [10, 20, 30]),
    ("len(set(xs))", [1, 2, 2, 3, 3, 3]), ("max(xs) - min(xs)", [4, 9, 1, 7]),
    ("sorted(xs)[-2]", [5, 1, 9, 3]), ("sum(x for x in xs if x % 2 == 0)", [1, 2, 3, 4, 5, 6]),
    ("len([x for x in xs if x > 2])", [1, 2, 3, 4, 5]), ("xs[::-1][0]", [7, 8, 9]),
    ("sum(xs) // len(xs)", [2, 4, 9]), ("min(xs[1:-1])", [1, 5, 2, 8, 0]),
    ("len(xs) * 2", [1, 2, 3]), ("sorted(xs)[0] + sorted(xs)[-1]", [6, 2, 8]),
    ("sum(xs[:2])", [4, 5, 6, 7]), ("len(str(sum(xs)))", [40, 50, 30]),
    ("xs.index(max(xs))", [3, 9, 4]),
    ("sum(xs) - max(xs)", [5, 10, 15]), ("len(xs[2:])", [1, 2, 3, 4, 5, 6]),
    ("sorted(xs, reverse=True)[1]", [4, 11, 7]), ("max(xs) * len(xs)", [2, 3, 4]),
    ("sum(sorted(xs)[:2])", [9, 1, 5, 3]),
    ("len([x for x in xs if x % 3 == 0])", [3, 6, 7, 9, 10]),
    ("min(xs) + max(xs)", [8, 2, 5]), ("sum(xs) % len(xs)", [7, 8, 9, 10]),
    ("len(set(xs)) * 3", [1, 1, 2, 2, 5]), ("sorted(xs)[len(xs) - 1]", [12, 4, 8]),
    ("abs(xs[0] - xs[-1])", [3, 99, 10]), ("sum(x * 2 for x in xs)", [1, 2, 3]),
    ("len(xs) + max(xs)", [6, 6, 6]), ("sorted(set(xs))[1]", [4, 4, 2, 9]),
    ("sum(xs[::2])", [1, 2, 3, 4, 5]),
]

#: (algorithm, the notation its worst case is written in)
_COMPLEXITY = [
    ("binary search on a sorted array", r"o\s*\(?\s*log", "O(log n)"),
    ("looking a key up in a hash table, on average", r"o\s*\(?\s*1", "O(1)"),
    ("merge sort", r"o\s*\(?\s*n\s*\*?\s*log", "O(n log n)"),
    ("bubble sort", r"o\s*\(?\s*n\s*(\^|\*\*)?\s*2", "O(n^2)"),
    ("finding the largest item in an unsorted list", r"o\s*\(?\s*n\b", "O(n)"),
    ("breadth-first search over V vertices and E edges", r"v\s*\+\s*e", "O(V + E)"),
    ("quicksort", r"o\s*\(?\s*n\s*(\^|\*\*)?\s*2", "O(n^2)"),
    ("appending to a dynamic array, amortised", r"o\s*\(?\s*1", "O(1)"),
    ("linear search through an unsorted list", r"o\s*\(?\s*n\b", "O(n)"),
    ("heap sort", r"o\s*\(?\s*n\s*\*?\s*log", "O(n log n)"),
]

#: (what to do, the fragment the command must contain)
_GIT = [
    ("undo the most recent commit while keeping its changes staged", "reset --soft",
     "git reset --soft HEAD~1"),
    ("discard every uncommitted change in the working tree", "checkout",
     "git checkout -- ."),
    ("create a new branch and switch to it in one command", "checkout -b",
     "git checkout -b my-branch"),
    ("show the commit history one line per commit", "--oneline", "git log --oneline"),
    ("change the message of the most recent commit", "--amend", "git commit --amend"),
    ("temporarily set aside uncommitted changes", "stash", "git stash"),
    ("show which files differ from the last commit", "status", "git status"),
    ("bring a single commit from another branch onto this one", "cherry-pick",
     "git cherry-pick abc1234"),
    ("list every branch in the repository", "branch", "git branch"),
    ("undo a commit by making a new commit that reverses it", "revert",
     "git revert abc1234"),
    ("download new commits from the remote without merging them", "fetch", "git fetch"),
    ("send your local commits to the remote", "push", "git push"),
    ("show the changes you have staged for the next commit", "--staged",
     "git diff --staged"),
    ("combine another branch's history into the current branch", "merge",
     "git merge other-branch"),
    ("see who last changed each line of a file", "blame", "git blame file.py"),
    ("take a file out of staging without deleting it", "restore --staged",
     "git restore --staged file.py"),
]

#: (what the pattern must match, examples it must accept, examples it must reject)
_PATTERNS = [
    ("a date in the form YYYY-MM-DD", ("2026-09-06", "1999-12-31"),
     ("2026-9-6", "26-09-06", "not-a-date"), r"\d{4}-\d{2}-\d{2}"),
    ("a 24-hour time in the form HH:MM", ("09:30", "23:59"), ("9:30", "24:00:00", "noon"),
     r"([01]\d|2[0-3]):[0-5]\d"),
    ("a six-digit hex colour starting with #", ("#a1b2c3", "#FFFFFF"),
     ("a1b2c3", "#fff", "#12345g"), r"#[0-9a-fA-F]{6}"),
    ("a UK-style postcode outward code such as SW1A", ("SW1A", "M1"), ("sw1a1aa", "12345"),
     r"[A-Z]{1,2}\d[A-Z\d]?"),
    ("a string of exactly four digits", ("1234", "0000"), ("123", "12345", "12a4"),
     r"\d{4}"),
    ("an IPv4 address with four dot-separated numbers", ("192.168.0.1", "8.8.8.8"),
     ("192.168.0", "1.2.3.4.5", "hello"), r"\d{1,3}(\.\d{1,3}){3}"),
    ("a word made only of lowercase letters", ("hello", "abc"), ("Hello", "abc1", "a b")
     , r"[a-z]+"),
    ("a positive integer with no leading zero", ("42", "7"), ("042", "-3", "3.5"),
     r"[1-9]\d*"),
    ("a lowercase word of exactly three letters", ("cat", "dog"), ("cats", "ca", "Cat"),
     r"[a-z]{3}"),
    ("a year between 1900 and 1999", ("1900", "1999"), ("2000", "1899", "19999"),
     r"19\d{2}"),
    ("a price with exactly two decimal places", ("12.50", "0.99"),
     ("12.5", "12", "12.500"), r"\d+\.\d{2}"),
    ("a UK mobile number starting 07 with eleven digits in total", ("07123456789",),
     ("0712345678", "17123456789", "07 123"), r"07\d{9}"),
    ("one or more digits separated by single commas", ("1,2,3", "10"),
     ("1, 2", "1,,2", "a,b"), r"\d+(,\d+)*"),
    ("a capital letter followed by exactly two digits", ("A12", "Z00"),
     ("a12", "A1", "A123"), r"[A-Z]\d{2}"),
]

#: (function name, what it does, arity, idioms any correct answer will contain)
_FUNCTIONS = [
    ("reverse_words", "returns the words of text in reverse order, separated by single "
     "spaces", ("text",), ("split",), ("[::-1]", "reversed", ".reverse(", "insert(0"),
     "def reverse_words(text):\n    return ' '.join(text.split()[::-1])"),
    ("safe_divide", "returns a divided by b, or None when b is zero", ("a", "b"),
     ("None",), ("/",),
     "def safe_divide(a, b):\n    if b == 0:\n        return None\n    return a / b"),
    ("count_vowels", "returns how many vowels are in text", ("text",), (),
     ("for", "sum", "count"),
     "def count_vowels(text):\n    return sum(1 for c in text.lower() if c in 'aeiou')"),
    ("is_palindrome", "returns True when text reads the same backwards, ignoring case",
     ("text",), ("lower",), ("[::-1]", "reversed"),
     "def is_palindrome(text):\n    t = text.lower()\n    return t == t[::-1]"),
    ("largest", "returns the largest number in a list, or None when the list is empty",
     ("items",), ("None",), ("max", "for"),
     "def largest(items):\n    if not items:\n        return None\n    return max(items)"),
    ("initials", "returns the first letter of each word in name, uppercased and joined",
     ("name",), ("split",), ("upper", "join"),
     "def initials(name):\n    return ''.join(w[0].upper() for w in name.split())"),
    ("clamp", "returns value limited to between low and high", ("value", "low", "high"),
     (), ("min", "max", "if"),
     "def clamp(value, low, high):\n    return max(low, min(value, high))"),
    ("unique", "returns the items of a list without duplicates, keeping their order",
     ("items",), (), ("for", "set", "dict"),
     "def unique(items):\n    seen = set()\n    out = []\n    for i in items:\n"
     "        if i not in seen:\n            seen.add(i)\n            out.append(i)\n"
     "    return out"),
    ("word_count", "returns how many words are in text", ("text",), ("split",), ("len",),
     "def word_count(text):\n    return len(text.split())"),
    ("second_largest", "returns the second largest distinct number in a list",
     ("items",), (), ("sorted", "set", "max"),
     "def second_largest(items):\n    return sorted(set(items))[-2]"),
    ("title_case", "returns text with the first letter of every word capitalised",
     ("text",), (), ("title", "upper", "capitalize", "split"),
     "def title_case(text):\n    return text.title()"),
    ("sum_digits", "returns the sum of the digits of a positive whole number",
     ("number",), (), ("str", "%", "//", "digit"),
     "def sum_digits(number):\n    return sum(int(d) for d in str(number))"),
    ("longest_word", "returns the longest word in text", ("text",), ("split",),
     ("max", "sort", "len"),
     "def longest_word(text):\n    return max(text.split(), key=len)"),
    ("flatten", "returns one flat list from a list of lists", ("lists",), (),
     ("for", "extend", "sum", "chain"),
     "def flatten(lists):\n    out = []\n    for part in lists:\n"
     "        out.extend(part)\n    return out"),
    ("starts_with_vowel", "returns True when word begins with a vowel", ("word",),
     (), ("lower", "in", "aeiou"),
     "def starts_with_vowel(word):\n    return word[:1].lower() in 'aeiou'"),
    ("remove_spaces", "returns text with every space removed", ("text",), (),
     ("replace", "join", "split"),
     "def remove_spaces(text):\n    return text.replace(' ', '')"),
]

#: (what the query must return, the clauses it needs)
#: (what to return, tables it must name, clauses it must use, row limits, an answer)
_QUERIES = [
    ("the names of the 3 customers with the highest total order value",
     ("customers", "orders"), ("group by", "order by", "desc"),
     ("limit 3", "top 3", "fetch first 3"),
     "SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.id "
     "GROUP BY c.name ORDER BY SUM(o.total) DESC LIMIT 3"),
    ("how many orders each customer has placed",
     ("customers", "orders"), ("group by", "count"), (),
     "SELECT c.name, COUNT(o.id) FROM customers c JOIN orders o ON o.customer_id = c.id "
     "GROUP BY c.name"),
    ("the names of customers who have never placed an order",
     ("customers", "orders"), ("left join", "null"), (),
     "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id "
     "WHERE o.id IS NULL"),
    ("the total value of all orders",
     ("orders",), ("sum",), (),
     "SELECT SUM(total) FROM orders"),
    ("the names of customers whose orders total more than 500",
     ("customers", "orders"), ("group by", "having"), (),
     "SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.id "
     "GROUP BY c.name HAVING SUM(o.total) > 500"),
    ("every customer name in alphabetical order",
     ("customers",), ("order by",), (),
     "SELECT name FROM customers ORDER BY name"),
    ("the single largest order total",
     ("orders",), ("max",), (),
     "SELECT MAX(total) FROM orders"),
    ("the number of customers",
     ("customers",), ("count",), (),
     "SELECT COUNT(*) FROM customers"),
    ("the average order total",
     ("orders",), ("avg",), (),
     "SELECT AVG(total) FROM orders"),
    ("the 5 most recent orders by id, highest first",
     ("orders",), ("order by", "desc"), ("limit 5", "top 5", "fetch first 5"),
     "SELECT * FROM orders ORDER BY id DESC LIMIT 5"),
]


def _coding() -> Iterator[Task]:
    for index, (expression, values) in enumerate(_TRACES):
        # Evaluated here so the question and its answer cannot disagree.
        answer = eval(expression, {"__builtins__": {}}, {  # noqa: S307 - our own literals
            "xs": list(values), "sorted": sorted, "sum": sum, "len": len, "set": set,
            "max": max, "min": min, "str": str, "abs": abs, "int": int,
        })
        yield _number_task(
            id=f"coding.trace.{index}",
            use_case="coding",
            label="Reading unfamiliar code",
            prompt=(
                f"What does this Python return for the input {values}?\n"
                f"def f(xs):\n    return {expression}\n"
                "Reply with only the value."
            ),
            answer=answer,
            rubric=f"{answer}. Evaluating {expression} over {values}.",
            max_tokens=300,
        )

    for index, (algorithm, pattern, shown) in enumerate(_COMPLEXITY):
        yield Task(
            id=f"coding.complexity.{index}",
            use_case="coding",
            label=f"Cost of {algorithm.split(' ')[0]}",
            prompt=(
                f"What is the worst-case time complexity of {algorithm}? "
                "Reply with only the big-O notation."
            ),
            system=TERSE_SYSTEM,
            max_tokens=200,
            rubric=f"{shown}.",
            canonical=shown,
            checks=(
                # A pattern rather than a needle: "log n" is a substring of "n log n",
                # so a Contains check cannot tell merge sort from binary search.
                Matches(label=f"Got {shown}", pattern=pattern,
                        detail=f"Expected {shown}.", weight=3.0),
                Terse(label="Answered with just the notation", max_words=10),
            ),
        )

    for index, (what, fragment, shown) in enumerate(_GIT):
        yield Task(
            id=f"coding.git.{index}",
            use_case="coding",
            label=f"Command to {what.split(' ')[0]}",
            prompt=f"Which git command will {what}? Reply with only the command.",
            system=TERSE_SYSTEM,
            max_tokens=200,
            rubric=f"{shown}.",
            canonical=shown,
            checks=(
                Contains(label=f"Uses {fragment}", needles=(fragment,), weight=3.0),
                Terse(label="Answered with just the command", max_words=12),
            ),
        )

    for index, (what, accept, reject, shown) in enumerate(_PATTERNS):
        yield Task(
            id=f"coding.regex.{index}",
            use_case="coding",
            label=f"Pattern for {what.split(' ')[1] if ' ' in what else what}",
            prompt=(
                f"Write a regular expression that matches {what} and nothing else. "
                "Reply with only the pattern, on one line."
            ),
            system=CODE_SYSTEM,
            max_tokens=250,
            rubric=f"Matches {', '.join(accept)} and rejects {', '.join(reject)}.",
            canonical=shown,
            checks=(
                RegexAnswer(label="Matches the right things and rejects the rest",
                            should_match=accept, should_reject=reject, weight=3.0),
            ),
        )

    for index, (name, what, args, must, idioms, shown) in enumerate(_FUNCTIONS):
        checks: list[Any] = [
            IsPython(label="Real Python that parses", function=name, arity=len(args),
                     weight=3.0)
        ]
        if must:
            checks.append(Contains(label=f"Uses {must[0]}", needles=must, weight=1.5))
        if idioms:
            checks.append(
                Contains(label="Does the work", needles=idioms, any_of=True, weight=2.0)
            )
        checks.append(
            Excludes(label="No commentary around it",
                     needles=("Here is", "Here's", "This function"))
        )
        yield Task(
            id=f"coding.function.{index}",
            use_case="coding",
            label=f"Writing {name}()",
            prompt=(
                f"Write a Python function {name}({', '.join(args)}) that {what}. "
                "Reply with only the code."
            ),
            system=CODE_SYSTEM,
            max_tokens=400,
            rubric=f"Real Python defining {name} with {len(args)} argument(s) that {what}.",
            canonical=shown,
            checks=tuple(checks),
        )

    for index, (what, tables, clauses, limits, shown) in enumerate(_QUERIES):
        checks = [
            Contains(label="Uses the right tables", needles=tables, weight=1.5)
        ]
        for clause in clauses:
            checks.append(
                Contains(label=f"Uses {clause.upper()}", needles=(clause,), weight=2.0)
            )
        if limits:
            checks.append(
                Contains(label="Limits the rows", needles=limits, any_of=True, weight=1.5)
            )
        yield Task(
            id=f"coding.sql.{index}",
            use_case="coding",
            label="Writing a query",
            prompt=(
                "Tables: customers(id, name) and orders(id, customer_id, total). "
                f"Write one SQL query returning {what}. Reply with only the query."
            ),
            system=CODE_SYSTEM,
            max_tokens=300,
            rubric=f"One query returning {what}.",
            canonical=shown,
            checks=tuple(checks),
        )

    # Spotting the bug in code that is nearly right.
    bugs = [
        ("def largest(items):\n    best = 0\n    for item in items:\n"
         "        if item > best:\n            best = item\n    return best",
         "the largest number in a list",
         ("negative", "below zero", "less than zero", "initial", "initialis", "initializ",
          "starts at 0", "starting at 0", "first element", "-inf", "empty"),
         "It returns 0 when every number in the list is negative."),
        ("def average(items):\n    return sum(items) / len(items)",
         "the average of a list",
         ("empty", "zero", "divide", "division"),
         "It divides by zero when the list is empty."),
        ("def first_word(text):\n    return text.split(' ')[0]",
         "the first word of a string",
         ("empty", "whitespace", "space", "blank", "leading"),
         "It returns an empty string when the text has leading spaces."),
        ("def contains(items, target):\n    for item in items:\n"
         "        if item == target:\n            return True\n        return False",
         "whether a list contains a value",
         ("indent", "return", "first", "early", "loop"),
         "The return False is indented inside the loop, so it only checks the first item."),
        ("def double_all(items):\n    for i in range(len(items)):\n"
         "        items[i] = items[i] * 2\n    return items",
         "a list with every item doubled",
         ("mutat", "in place", "in-place", "modifies", "original", "copy"),
         "It mutates the caller's list in place instead of returning a new one."),
        ("def add_item(item, basket=[]):\n    basket.append(item)\n    return basket",
         "a basket with the item added",
         ("default", "mutable", "shared", "same list", "persists"),
         "The default list is created once and shared between every call."),
        ("def is_even(n):\n    if n % 2 == 0:\n        return True",
         "whether a number is even",
         ("none", "odd", "return", "missing", "implicit"),
         "It returns None instead of False for odd numbers."),
        ("def last(items):\n    return items[len(items)]",
         "the last item of a list",
         ("index", "out of range", "off by one", "off-by-one", "len(items) - 1", "error"),
         "It indexes one past the end; the last index is len(items) - 1."),
        ("def percent(part, whole):\n    return part / whole * 100",
         "a percentage",
         ("zero", "divide", "division"),
         "It divides by zero when whole is 0."),
        ("def count_up(n):\n    for i in range(1, n):\n        print(i)",
         "the numbers from 1 to n",
         ("range", "excl", "misses", "stops", "off by one", "off-by-one"),
         "range(1, n) stops at n - 1, so it never prints n."),
    ]
    for index, (code, what, needles, shown) in enumerate(bugs):
        yield Task(
            id=f"coding.bug.{index}",
            use_case="coding",
            label="Spotting a bug",
            prompt=(
                f"This Python is meant to return {what} but is wrong:\n{code}\n"
                "In one sentence, what is the bug? Reply with only that sentence."
            ),
            system=TERSE_SYSTEM,
            max_tokens=250,
            rubric=f"Names the fault. For example: {shown}",
            canonical=shown,
            checks=(
                Contains(label="Names the fault", needles=needles, any_of=True, weight=3.0),
                SentencesAtMost(label="Kept it to one sentence", limit=1),
            ),
        )


# ------------------------------------------------------------------------- structured

_PEOPLE = [
    ("Priya Raman", 34, "Chennai", "architect"), ("Tomas Silva", 51, "Lisbon", "baker"),
    ("Aiko Tanaka", 27, "Osaka", "nurse"), ("Nadia Haddad", 43, "Beirut", "teacher"),
    ("Owen Blake", 19, "Cardiff", "student"), ("Mei Lin", 62, "Taipei", "engineer"),
    ("Jonas Weber", 38, "Hamburg", "chef"), ("Ana Duarte", 45, "Porto", "dentist"),
    ("Ravi Kapoor", 29, "Jaipur", "pilot"), ("Sofia Rossi", 56, "Bologna", "vet"),
    ("Liam Doyle", 33, "Galway", "farmer"), ("Yara Costa", 24, "Recife", "designer"),
    ("Hana Novak", 47, "Brno", "lawyer"), ("Diego Marin", 39, "Seville", "plumber"),
    ("Amara Okafor", 31, "Enugu", "doctor"), ("Petra Jansen", 58, "Utrecht", "florist"),
    ("Kwame Mensah", 22, "Kumasi", "cyclist"), ("Lena Fischer", 41, "Graz", "editor"),
    ("Arjun Nair", 36, "Kochi", "guitarist"), ("Freya Olsen", 49, "Bergen", "captain"),
]

_BASKETS = [
    (("pen", 2), ("book", 15), ("lamp", 40)), (("mug", 6), ("kettle", 25), ("tray", 9)),
    (("hat", 12), ("scarf", 18), ("gloves", 14)), (("apple", 1), ("melon", 4), ("fig", 3)),
    (("shirt", 20), ("belt", 11), ("sock", 4)), (("rope", 8), ("hook", 5), ("clamp", 13)),
    (("torch", 17), ("battery", 3), ("bulb", 6)), (("bowl", 7), ("plate", 5), ("fork", 2)),
    (("tent", 120), ("mat", 22), ("stove", 45)), (("brush", 9), ("paint", 28), ("tape", 4)),
    (("chair", 55), ("stool", 30), ("bench", 90)), (("pencil", 1), ("ruler", 3), ("eraser", 2)),
]

_EXACT_WORDS = ["BANANA", "TANGERINE", "OCTOPUS", "MARIGOLD", "QUARTZ", "PELICAN",
                "THIMBLE", "LANTERN", "WALNUT", "SAFFRON", "JUNIPER", "OBSIDIAN",
                "FLAMINGO", "CARDAMOM", "TRELLIS", "MERIDIAN", "ZEPHYR", "CINNABAR"]


def _structured() -> Iterator[Task]:
    for index, (name, age, city, job) in enumerate(_PEOPLE):
        yield Task(
            id=f"structured.json.{index}",
            use_case="structured",
            label="Pulling fields out of a sentence",
            prompt=(
                f'From this sentence, return a JSON object with the keys name, city and '
                f'age: "{name} is {age} and lives in {city}." Reply with only the JSON.'
            ),
            system=TERSE_SYSTEM,
            max_tokens=250,
            rubric=f"Valid JSON with name, city {city} and age {age}.",
            canonical=f'{{"name": "{name}", "city": "{city}", "age": {age}}}',
            checks=(
                IsJson(label="Valid JSON with the right values",
                       keys=("name", "city", "age"),
                       values=(("city", city), ("age", str(age))), weight=3.0),
            ),
        )
        yield Task(
            id=f"structured.nested.{index}",
            use_case="structured",
            label="Nesting a structure",
            prompt=(
                'Return JSON with a key "person" whose value is an object with keys name '
                f'and job, where name is "{name}" and job is "{job}". '
                "Reply with only the JSON."
            ),
            system=TERSE_SYSTEM,
            max_tokens=250,
            rubric=f'JSON shaped {{"person": {{"name": "{name}", "job": "{job}"}}}}.',
            canonical=f'{{"person": {{"name": "{name}", "job": "{job}"}}}}',
            checks=(
                IsJson(label="Valid JSON with a person object", keys=("person",),
                       weight=2.0),
                Contains(label="Carries the values asked for", needles=(name, job),
                         weight=2.0),
            ),
        )

    for index, basket in enumerate(_BASKETS):
        rows = "\n".join(f"{item},{price}" for item, price in basket)
        listed = ", ".join(f"a {item} costs {price}" for item, price in basket)
        yield Task(
            id=f"structured.csv.{index}",
            use_case="structured",
            label="Producing a table",
            prompt=(
                "Return these as CSV with a header row of item,price and no other text: "
                f"{listed}."
            ),
            system=TERSE_SYSTEM,
            max_tokens=200,
            rubric="A header row then one row per item. A space after the comma is fine.",
            canonical=f"item,price\n{rows}",
            checks=(
                Contains(label="Has the header asked for",
                         needles=("item,price", "item, price"), any_of=True, weight=2.0),
                LinesAtLeast(label="One row per item", minimum=len(basket) + 1, weight=2.0),
                Contains(label="Carries every item",
                         needles=tuple(item for item, _ in basket), weight=1.5),
                Excludes(label="No commentary around it", needles=("here", "csv:", "```")),
            ),
        )

    for index, word in enumerate(_EXACT_WORDS):
        yield Task(
            id=f"structured.exact.{index}",
            use_case="structured",
            label="Doing exactly as told",
            prompt=f"Reply with exactly the word {word} and nothing else.",
            system=TERSE_SYSTEM,
            max_tokens=100,
            rubric=f"The single word {word}, with nothing around it.",
            canonical=word,
            checks=(
                Equals(label=f"Said {word}", expected=(word.lower(),), weight=2.0),
                Terse(label="Said nothing else", max_words=1, weight=2.0),
            ),
        )

    for index, count in enumerate((3, 4, 5, 6, 3, 4, 5, 6, 3, 4, 5, 6, 3, 4)):
        subjects = ["a paperclip", "a rubber band", "an empty jar", "a shoelace",
                    "a wooden spoon", "an old newspaper", "a bucket", "a tennis ball",
                    "a brick", "a bedsheet", "a coat hanger", "a cardboard box",
                    "a length of string", "a plastic bottle"]
        subject = subjects[index]
        yield Task(
            id=f"structured.list.{index}",
            use_case="structured",
            label=f"Keeping to {count} items",
            prompt=(
                f"List exactly {count} uses for {subject}, as a numbered list. "
                "No introduction, no conclusion."
            ),
            system=TERSE_SYSTEM,
            max_tokens=250,
            rubric=f"{count} numbered lines and nothing else. The uses do not matter.",
            canonical="\n".join(f"{n}. Use number {n}" for n in range(1, count + 1)),
            checks=(
                LinesAtLeast(label=f"{count} numbered items", minimum=count, numbered=True,
                             weight=2.0),
                AtMostWords(label="Stayed brief", limit=count * 12),
                Excludes(label="No introduction",
                         needles=("here are", "sure,", "certainly")),
            ),
        )

    sequences = [
        ("the numbers one to five as digits", "1,2,3,4,5", ("1", "2", "3", "4", "5")),
        ("the first four even numbers as digits", "2,4,6,8", ("2", "4", "6", "8")),
        ("the numbers ten, twenty and thirty as digits", "10,20,30", ("10", "20", "30")),
        ("the first three multiples of five as digits", "5,10,15", ("5", "10", "15")),
        ("the numbers one to three as digits", "1,2,3", ("1", "2", "3")),
        ("the first four odd numbers as digits", "1,3,5,7", ("1", "3", "5", "7")),
        ("the numbers six to nine as digits", "6,7,8,9", ("6", "7", "8", "9")),
        ("the first three square numbers as digits", "1,4,9", ("1", "4", "9")),
        ("the numbers one hundred, two hundred and three hundred as digits",
         "100,200,300", ("100", "200", "300")),
        ("the first four multiples of three as digits", "3,6,9,12",
         ("3", "6", "9", "12")),
        ("the numbers two, four and eight as digits", "2,4,8", ("2", "4", "8")),
        ("the first three prime numbers as digits", "2,3,5", ("2", "3", "5")),
    ]
    for index, (what, shown, needles) in enumerate(sequences):
        yield Task(
            id=f"structured.plain.{index}",
            use_case="structured",
            label="Withholding formatting",
            prompt=(
                f"Write {what}, separated by commas, on one line. "
                "Use no markdown, no bullet points and no other text."
            ),
            system=TERSE_SYSTEM,
            max_tokens=150,
            rubric=f"{shown} on one line, with no markdown and nothing else.",
            canonical=shown,
            checks=(
                Contains(label="Has every number", needles=needles, weight=2.0),
                Excludes(label="No markdown", needles=("*", "#", "- ", "```")),
                Terse(label="Kept to one line", max_words=10),
            ),
        )


def _field_extraction() -> Iterator[Task]:
    """Answer with one field and nothing around it.

    Instruction-following at its barest: there is no room to be right and verbose at the
    same time, so the two things being measured cannot be confused for each other.
    """

    for index, (name, age, city, job) in enumerate(_PEOPLE):
        for field, value in (("city", city), ("job", job)):
            yield _word_task(
                id=f"structured.field.{index}.{field}",
                use_case="structured",
                label=f"Answering with only the {field}",
                prompt=(
                    f'"{name} is {age}, works as a {job} and lives in {city}." '
                    f"What is the {field}? Reply with only the {field} and nothing else."
                ),
                expected=(value.lower(),),
                rubric=f"{value}, with nothing around it.",
                max_tokens=150,
                terse_words=3,
            )


def _structured_all() -> Iterator[Task]:
    yield from _structured()
    yield from _field_extraction()


# ---------------------------------------------------------------------------- writing

_VERBOSE = [
    ("Due to the fact that it was raining very heavily, we made the decision to postpone "
     "the event until a later date.", 10, ("rain",),
     ("postpon", "delay", "moved", "reschedul", "put off", "later"),
     "Heavy rain forced us to postpone the event."),
    ("In light of the fact that the budget has been reduced, we are of the opinion that "
     "the project should be paused.", 10, ("budget",),
     ("paus", "stop", "halt", "hold", "suspend"),
     "The reduced budget means we should pause the project."),
    ("It has come to our attention that a number of customers have been experiencing "
     "difficulties when attempting to log in.", 10, ("customer", "log"),
     ("difficult", "trouble", "problem", "cannot", "can't", "fail"),
     "Some customers cannot log in."),
    ("At this moment in time we do not have the ability to provide a definitive answer "
     "to your question.", 10, (),
     ("cannot", "can't", "unable", "no answer", "not yet"),
     "We cannot answer that yet."),
    ("There is a strong likelihood that the shipment will arrive at some point during "
     "the course of next week.", 10, ("shipment", "week"),
     ("likely", "probabl", "expect", "should"),
     "The shipment will likely arrive next week."),
    ("We would like to take this opportunity to express our gratitude for the assistance "
     "that you provided to us.", 10, (), ("thank", "grateful", "appreciat"),
     "Thank you for your help."),
    ("Owing to circumstances beyond our control the delivery will not be arriving on the "
     "day that was originally agreed.", 10, ("deliver",),
     ("late", "delay", "not", "miss", "another day"),
     "The delivery will be late."),
    ("It is our recommendation that you should give consideration to renewing the policy "
     "prior to the expiry date.", 10, ("policy", "renew"),
     ("renew", "before", "expir"),
     "Renew the policy before it expires."),
    ("A decision has been taken by the committee to the effect that the proposal will not "
     "be proceeding any further.", 10, ("proposal", "committee"),
     ("reject", "not", "stop", "declin", "turned down"),
     "The committee rejected the proposal."),
    ("We are currently in the process of undertaking a review of the way in which our "
     "opening hours are structured.", 10, ("hour", "open", "review"),
     ("review", "look", "chang"),
     "We are reviewing our opening hours."),
    ("Please be advised that it will be necessary for you to bring a form of photographic "
     "identification with you.", 10, ("photo", "id", "identification"),
     ("bring", "need", "must"),
     "Please bring photo identification."),
    ("There exists a possibility that some degree of disruption may be experienced by "
     "travellers during the weekend period.", 10, ("travel", "weekend", "disrupt"),
     ("disrupt", "delay", "may", "might", "possible"),
     "Travel may be disrupted at the weekend."),
    ("The management would like to remind all members of staff that the kitchen area "
     "must be left in a clean condition.", 10, ("kitchen", "clean", "staff"),
     ("clean", "tidy", "leave"),
     "Staff must leave the kitchen clean."),
    ("It has been determined that the most appropriate course of action would be to "
     "obtain a second opinion on the matter.", 10, ("opinion", "second"),
     ("second opinion", "another", "ask"),
     "We should get a second opinion."),
    ("Subsequent to the completion of the building work the car park will once again "
     "become available for general use.", 10, ("car park", "park", "build"),
     ("reopen", "open", "available", "after", "again"),
     "The car park reopens after the building work."),
    ("We wish to make you aware of the fact that the price quoted does not make any "
     "allowance for the cost of delivery.", 10, ("price", "deliver", "quote"),
     ("exclude", "not include", "extra", "separate", "excluding"),
     "The quoted price excludes delivery."),
    ("Please note that failure to attend the appointment may result in the appointment "
     "being cancelled without further notice.", 10, ("appointment", "attend"),
     ("miss", "cancel", "lose", "not attend"),
     "Miss the appointment and it may be cancelled."),
]

_RUDE = [
    ("Your report is a mess and you clearly did not bother to check it.",
     ("mess", "did not bother", "didn't bother", "sloppy", "careless"),
     "Could you please take another look at the report before we send it?"),
    ("This code is terrible and whoever wrote it should be ashamed.",
     ("terrible", "ashamed", "awful", "garbage"),
     "Could we revisit this code together and tidy a few things up?"),
    ("You are always late and it is extremely annoying for everyone.",
     ("annoying", "always late"),
     "Could you try to arrive on time, as it affects the rest of the team?"),
    ("Your proposal makes no sense and wastes everyone's time.",
     ("no sense", "wastes", "nonsense"),
     "Could you clarify a few points in the proposal so we can review it properly?"),
    ("Stop asking stupid questions and read the documentation.",
     ("stupid",), "The documentation covers this, and I am happy to point you to it."),
    ("This design is ugly and nobody will want to use it.",
     ("ugly", "nobody"),
     "Could we explore a few alternative designs before settling on this one?"),
    ("Your estimate is completely unrealistic and shows you have no idea how long this takes.",
     ("unrealistic", "no idea"),
     "Could we walk through the estimate together, as it looks tight to me?"),
    ("Nobody reads your updates because they are far too long and rambling.",
     ("nobody", "rambling", "far too long"),
     "Could the updates be a little shorter so they are easier to scan?"),
    ("You broke the build again and did not even check before pushing.",
     ("broke", "did not even", "again"),
     "The build is failing; could you check the tests before pushing next time?"),
    ("This meeting was pointless and achieved absolutely nothing.",
     ("pointless", "nothing"),
     "Could we set a clearer agenda for the next meeting so it is more useful?"),
    ("Your handwriting is illegible and I gave up trying to read it.",
     ("illegible", "gave up"),
     "Could you send that through typed, so I can read it more easily?"),
    ("You never reply to emails and it makes working with you impossible.",
     ("never", "impossible"),
     "Could you let me know when you have had a chance to read my email?"),
    ("The kitchen is disgusting and whoever left it that way is inconsiderate.",
     ("disgusting", "inconsiderate"),
     "Could everyone please tidy the kitchen after using it?"),
    ("Your slides are boring and full of mistakes.",
     ("boring", "full of mistakes"),
     "Could we tighten the slides and give them a proofread before Friday?"),
    ("This is the third time I have had to explain this to you.",
     ("third time",),
     "Would it help if I wrote this down so you have it to refer back to?"),
]

_JARGON = [
    ("We will leverage synergies to operationalise a best-in-class paradigm.",
     ("leverage", "synergy", "synergies", "operationalise", "operationalize", "paradigm",
      "best-in-class"),
     "We will work together to build something excellent."),
    ("Let us circle back and touch base offline to action the deliverables.",
     ("circle back", "touch base", "offline", "action the", "deliverables"),
     "Let us talk later about what needs doing."),
    ("Going forward we need to ideate low-hanging fruit and move the needle.",
     ("going forward", "ideate", "low-hanging", "move the needle"),
     "From now on we need easy ideas that make a real difference."),
    ("Our core competency is delivering value-add at scale across the ecosystem.",
     ("core competency", "value-add", "at scale", "ecosystem"),
     "We are good at helping lots of people at once."),
    ("We should socialise the roadmap with stakeholders to drive alignment.",
     ("socialise", "socialize", "stakeholders", "drive alignment"),
     "We should share the plan with the team so everyone agrees."),
    ("Let us take a helicopter view before we drill down into the granular detail.",
     ("helicopter view", "drill down", "granular"),
     "Let us look at the whole thing before examining the details."),
    ("We need to right-size the team and sunset the legacy workstream.",
     ("right-size", "sunset", "workstream"),
     "We need a smaller team and should stop the old project."),
    ("This initiative will unlock significant white space in the addressable market.",
     ("unlock", "white space", "addressable market"),
     "This will let us reach many customers nobody is serving."),
    ("Let us park that and take it offline with the relevant stakeholders.",
     ("park that", "offline", "stakeholders"),
     "Let us leave that for now and discuss it separately with the people involved."),
    ("We are pivoting to a customer-centric model to maximise mindshare.",
     ("pivoting", "customer-centric", "mindshare"),
     "We are changing direction to focus on customers so more people know us."),
    ("Our north star metric should cascade down to every squad's OKRs.",
     ("north star", "cascade", "squad", "okr"),
     "Every team's goals should follow from the one measure that matters most."),
    ("We must double-click on the pain points surfaced during discovery.",
     ("double-click", "pain points", "surfaced", "discovery"),
     "We must look closely at the problems we found while researching."),
    ("Bandwidth permitting, we will onboard the vendor in the next sprint.",
     ("bandwidth", "onboard"),
     "If we have time, we will start working with the supplier in the next few weeks."),
    ("This is a paradigm shift that will disrupt the incumbent value chain.",
     ("paradigm shift", "disrupt", "incumbent", "value chain"),
     "This changes how the whole industry works."),
]

_STORIES = [
    ("a dog that learned to ride a bus to the park alone", 6, ("dog", "bus"),
     "Dog rides bus to park alone"),
    ("a library that stayed open all night for one reader", 6, ("library", "reader",
     "night"), "Library opens all night for one"),
    ("a lost cat that walked home across three counties", 6, ("cat", "home"),
     "Lost cat walks home across counties"),
    ("a baker who gave away bread during a storm", 6, ("baker", "bread", "storm"),
     "Baker gives away bread in storm"),
    ("a child who built a working radio from scrap", 6, ("child", "radio"),
     "Child builds working radio from scrap"),
    ("a train driver who stopped for a duck", 6, ("train", "duck"),
     "Train driver stops for a duck"),
    ("a village that grew its own woodland in ten years", 6, ("village", "wood", "forest",
     "tree"), "Village grows its own woodland"),
    ("a postman who delivered mail by kayak", 6, ("post", "mail", "kayak"),
     "Postman delivers mail by kayak"),
    ("a school where every pupil learns to swim", 6, ("school", "swim", "pupil"),
     "School teaches every pupil to swim"),
    ("a farmer who invented a better gate latch", 6, ("farmer", "gate", "latch"),
     "Farmer invents a better gate latch"),
    ("a bridge repaired by volunteers in one weekend", 6, ("bridge", "volunteer",
     "weekend"), "Volunteers repair bridge in a weekend"),
    ("a bookshop that survived by selling soup", 6, ("bookshop", "soup", "book"),
     "Bookshop survives by selling soup"),
    ("a museum that let visitors touch everything", 6, ("museum", "touch", "visitor"),
     "Museum lets visitors touch everything"),
    ("a beekeeper who mapped every hive in the county", 6, ("bee", "hive", "map"),
     "Beekeeper maps every hive in county"),
    ("a lighthouse switched back on after fifty years", 6, ("lighthouse", "year"),
     "Lighthouse shines again after fifty years"),
    ("a runner who finished last but kept going", 6, ("runner", "last", "finish"),
     "Runner finishes last but keeps going"),
    ("a cafe that pays its staff to read", 6, ("cafe", "staff", "read"),
     "Cafe pays its staff to read"),
]

_DELAYS = [
    ("Friday's release is delayed to Monday", ("delay", "postpon", "moved", "monday",
     "slip", "push"), "Release moved from Friday to Monday"),
    ("the office will be closed on Thursday for maintenance", ("closed", "thursday",
     "maintenance"), "Office closed Thursday for maintenance"),
    ("the team meeting has moved from 2pm to 4pm", ("moved", "meeting", "4"),
     "Team meeting moved to 4pm"),
    ("the printer on floor three is broken", ("printer", "broken", "floor"),
     "Floor three printer is broken"),
    ("everyone must change their password this week", ("password", "change", "week"),
     "Change your password this week"),
    ("the car park will be resurfaced next month", ("car park", "park", "resurfac",
     "month"), "Car park resurfacing starts next month"),
    ("the canteen is trialling a new supplier", ("canteen", "supplier", "trial"),
     "Canteen trials a new supplier"),
    ("the fire alarm will be tested on Wednesday", ("fire", "alarm", "test",
     "wednesday"), "Fire alarm test on Wednesday"),
    ("all laptops need a security update by Friday", ("laptop", "security", "update",
     "friday"), "Install the security update by Friday"),
    ("the client visit has been brought forward a week", ("client", "visit", "forward",
     "week", "earlier"), "Client visit moved a week earlier"),
    ("parking permits must be renewed this month", ("parking", "permit", "renew"),
     "Renew your parking permit this month"),
    ("the intranet will be offline on Sunday morning", ("intranet", "offline",
     "sunday"), "Intranet offline Sunday morning"),
    ("a new starter joins the design team on Monday", ("starter", "design", "monday",
     "join"), "New designer joins on Monday"),
    ("expenses must be submitted before the month ends", ("expense", "submit",
     "month"), "Submit expenses before month end"),
    ("the lift will be out of service for two days", ("lift", "service", "days"),
     "Lift out of service for two days"),
    ("the team photo is being taken on Thursday", ("photo", "thursday", "team"),
     "Team photo on Thursday"),
]

_PARAGRAPHS = [
    ("The city council voted on Tuesday to extend the tram line by four stops, adding "
     "service to the eastern suburbs by 2029. The extension will cost an estimated 240 "
     "million and was opposed by three councillors who argued the money would be better "
     "spent on bus frequency.", ("tram", "line", "extension"),
     "The council voted to extend the tram line by four stops by 2029."),
    ("A study of 4,000 office workers found that those who took a short walk every hour "
     "reported less back pain and better concentration than those who did not. The "
     "effect held regardless of age or how much exercise people took outside work.",
     ("walk", "hourly", "hour", "pain", "concentration"),
     "Hourly short walks reduced back pain and improved concentration for office workers."),
    ("The museum will return a collection of 200 artefacts to Nigeria next year, "
     "following a decade of negotiation. The pieces were taken during a punitive "
     "expedition in 1897 and have been in storage for most of the past fifty years.",
     ("museum", "artefact", "nigeria", "return"),
     "The museum will return 200 artefacts to Nigeria after a decade of talks."),
    ("Rail operators have agreed to simplify ticket pricing after research found that "
     "most passengers could not tell which of several fares was cheapest for the same "
     "journey. The change takes effect in the spring.",
     ("ticket", "fare", "pricing", "rail", "simplif"),
     "Rail operators will simplify confusing ticket pricing in the spring."),
    ("A trial of a four-day week at 61 companies found that revenue held steady while "
     "staff reported less burnout. Nearly all of the companies chose to keep the "
     "arrangement after the trial ended.",
     ("four-day", "week", "trial", "burnout"),
     "A four-day week trial kept revenue steady and reduced burnout."),
    ("Coastal councils have begun planting marram grass along eroding dunes after a "
     "five-year study showed the roots hold sand in place more cheaply than concrete "
     "barriers.",
     ("dune", "grass", "eros", "coast", "sand"),
     "Councils are planting grass to hold eroding dunes cheaply."),
    ("The national library has digitised two million pages of local newspapers, making "
     "them searchable for the first time. Historians expect the archive to change what "
     "is known about nineteenth-century towns.",
     ("librar", "digitis", "digitiz", "newspaper", "archive"),
     "The library digitised two million newspaper pages, now searchable."),
    ("A hospital cut waiting times by a third after moving routine blood tests into "
     "pharmacies. The scheme cost less than expected and is being copied by four "
     "neighbouring trusts.",
     ("hospital", "waiting", "blood", "pharmac"),
     "Moving blood tests to pharmacies cut hospital waiting times by a third."),
]


def _writing() -> Iterator[Task]:
    for index, (source, limit, keep, gist, shown) in enumerate(_VERBOSE):
        checks: list[Any] = [
            AtMostWords(label=f"At most {limit} words", limit=limit, weight=3.0)
        ]
        if keep:
            checks.append(Contains(label="Kept the subject", needles=keep, any_of=True,
                                   weight=1.5))
        checks.append(Contains(label="Kept the point", needles=gist, any_of=True,
                               weight=1.5))
        yield Task(
            id=f"writing.shorten.{index}",
            use_case="writing",
            label="Cutting a sentence down",
            prompt=(
                f'Rewrite this in at most {limit} words without losing the meaning: '
                f'"{source}" Reply with only the rewrite.'
            ),
            system=TERSE_SYSTEM,
            max_tokens=150,
            rubric=f"{limit} words or fewer, keeping the meaning.",
            canonical=shown,
            checks=tuple(checks),
        )

    for index, (source, rude, shown) in enumerate(_RUDE):
        yield Task(
            id=f"writing.tone.{index}",
            use_case="writing",
            label="Changing the tone",
            prompt=(
                f'Rewrite this politely, in one sentence: "{source}" '
                "Reply with only the rewrite."
            ),
            system=TERSE_SYSTEM,
            max_tokens=200,
            rubric="One polite sentence that keeps the request but drops the insult.",
            canonical=shown,
            checks=(
                Excludes(label="Dropped the rudeness", needles=rude, weight=3.0),
                Contains(label="Sounds polite",
                         needles=("please", "could", "would", "thank", "appreciate",
                                  "perhaps", "might", "kindly", "happy to"),
                         any_of=True, weight=1.5),
                SentencesAtMost(label="Kept it to one sentence", limit=1),
                AtMostWords(label="Stayed brief", limit=45),
            ),
        )

    for index, (source, jargon, shown) in enumerate(_JARGON):
        yield Task(
            id=f"writing.plain.{index}",
            use_case="writing",
            label="Removing jargon",
            prompt=(
                f'Rewrite this in plain English, one sentence: "{source}" '
                "Reply with only the rewrite."
            ),
            system=TERSE_SYSTEM,
            max_tokens=150,
            rubric="One sentence with none of the jargon words left in it.",
            canonical=shown,
            checks=(
                Excludes(label="Jargon gone", needles=jargon, weight=3.0),
                SentencesAtMost(label="Kept it to one sentence", limit=1),
            ),
        )

    for index, (premise, limit, about, shown) in enumerate(_STORIES):
        yield Task(
            id=f"writing.headline.{index}",
            use_case="writing",
            label="Writing to a constraint",
            prompt=(
                f"Write a {limit}-word headline for a story about {premise}. "
                "Reply with only the headline."
            ),
            system=TERSE_SYSTEM,
            max_tokens=120,
            rubric=f"{limit} words or fewer, and about the story.",
            canonical=shown,
            checks=(
                AtMostWords(label=f"{limit} words or fewer", limit=limit, weight=3.0),
                Contains(label="About the story", needles=about, any_of=True, weight=2.0),
            ),
        )

    for index, (news, about, shown) in enumerate(_DELAYS):
        yield Task(
            id=f"writing.subject.{index}",
            use_case="writing",
            label="Writing a subject line",
            prompt=(
                "Write an email subject line, at most 8 words, for a message telling a "
                f"team that {news}. Reply with only the subject line."
            ),
            system=TERSE_SYSTEM,
            max_tokens=120,
            rubric="Eight words or fewer, saying what happened.",
            canonical=shown,
            checks=(
                AtMostWords(label="At most 8 words", limit=8, weight=2.0),
                Contains(label="Says what happened", needles=about, any_of=True,
                         weight=2.0),
            ),
        )

    for index, (paragraph, about, shown) in enumerate(_PARAGRAPHS):
        for limit in (25, 20, 15):
            yield Task(
                id=f"writing.summarise.{index}.{limit}",
                use_case="writing",
                label=f"Summarising to {limit} words",
                prompt=(
                    f"Summarise this in exactly one sentence of at most {limit} words:\n"
                    f"{paragraph}"
                ),
                system=TERSE_SYSTEM,
                max_tokens=200,
                rubric=f"One sentence, {limit} words or fewer, keeping the point.",
                canonical=" ".join(shown.split()[:limit]),
                checks=(
                    AtMostWords(label=f"Within {limit} words", limit=limit, weight=2.0),
                    SentencesAtMost(label="One sentence", limit=1, weight=2.0),
                    Contains(label="Kept the point", needles=about, any_of=True,
                             weight=2.0),
                ),
            )


# ------------------------------------------------------------------------------- chat

#: Capitals people commonly get wrong, which is the point of asking.
_CAPITALS = [
    ("Australia", "Canberra"), ("Turkey", "Ankara"), ("Brazil", "Brasilia"),
    ("Canada", "Ottawa"), ("Switzerland", "Bern"), ("New Zealand", "Wellington"),
    ("Morocco", "Rabat"), ("Myanmar", "Naypyidaw"), ("Bolivia", "Sucre"),
    ("Nigeria", "Abuja"), ("Tanzania", "Dodoma"), ("Kazakhstan", "Astana"),
    ("Ivory Coast", "Yamoussoukro"), ("Sri Lanka", "Colombo"), ("Pakistan", "Islamabad"),
    ("United States", "Washington"), ("India", "Delhi"), ("Vietnam", "Hanoi"),
    ("South Africa", "Pretoria"), ("Bhutan", "Thimphu"), ("Mongolia", "Ulaanbaatar"),
    ("Ethiopia", "Addis Ababa"), ("Norway", "Oslo"), ("Croatia", "Zagreb"),
    ("Peru", "Lima"), ("Malaysia", "Kuala Lumpur"), ("Ghana", "Accra"),
    ("Iceland", "Reykjavik"), ("Nepal", "Kathmandu"), ("Uruguay", "Montevideo"),
]

_EVENTS = [
    ("the Berlin Wall fall", 1989), ("the first human walk on the Moon", 1969),
    ("the Titanic sink", 1912), ("the Second World War end", 1945),
    ("the World Wide Web become publicly available", 1991),
    ("the Chernobyl disaster happen", 1986), ("the Soviet Union dissolve", 1991),
    ("the euro enter circulation as notes and coins", 2002),
    ("the first iPhone go on sale", 2007), ("India gain independence", 1947),
    ("the first successful powered aeroplane flight take place", 1903),
    ("the Apollo 13 mission launch", 1970),
    ("the Channel Tunnel open to the public", 1994),
    ("the first modern Olympic Games take place in Athens", 1896),
    ("Nelson Mandela leave prison", 1990),
    ("the Hubble Space Telescope launch", 1990),
    ("the first Harry Potter book get published", 1997),
    ("the Great Fire of London happen", 1666),
]

_FACTS = [
    ("the largest planet in the Solar System", ("jupiter",)),
    ("the chemical symbol for gold", ("au",)),
    ("the longest river in Africa", ("nile",)),
    ("the largest ocean on Earth", ("pacific",)),
    ("the hardest naturally occurring mineral", ("diamond",)),
    ("the closest star to Earth", ("sun", "sol")),
    ("the smallest prime number", ("2", "two")),
    ("the gas plants absorb from the air to photosynthesise",
     ("carbon dioxide", "co2")),
    ("the metal that is liquid at room temperature", ("mercury",)),
    ("the largest mammal on Earth", ("blue whale", "whale")),
    ("the chemical symbol for iron", ("fe",)),
    ("the largest desert on Earth by area", ("antarctica", "antarctic", "sahara")),
    ("the number of sides on a hexagon", ("6", "six")),
    ("the tallest mountain above sea level", ("everest",)),
    ("the country that gave the Statue of Liberty to the United States", ("france",)),
    ("the language with the most native speakers", ("mandarin", "chinese")),
    ("the organ that pumps blood around the body", ("heart",)),
    ("the planet known as the red planet", ("mars",)),
    ("the process by which plants make food from sunlight", ("photosynthesis",)),
    ("the freezing point of water in degrees Celsius", ("0", "zero")),
]

_UNKNOWABLE = [
    "What did I have for breakfast this morning?",
    "What colour are the walls in my bedroom?",
    "How many unread emails do I have right now?",
    "What is my dog's name?",
    "Where did I park my car this morning?",
    "What time did I go to bed last night?",
    "How many people are in the room with me?",
    "What is written on the first page of my notebook?",
]

_EXPLAIN = [
    ("a database index", 30, ("look", "find", "faster", "quick", "book", "search",
     "index card"), ("b-tree", "btree", "cardinality", "query planner"),
     "It is like a book's index: it lets the computer find rows without reading every page."),
    ("what a firewall does", 30, ("block", "allow", "traffic", "guard", "filter", "door"),
     ("stateful", "packet inspection", "iptables"),
     "It is a guard on your network that decides which traffic is allowed in and out."),
    ("what a compiler does", 30, ("translat", "convert", "machine", "code", "language"),
     ("abstract syntax tree", "intermediate representation"),
     "It turns the code a person writes into instructions a computer can run directly."),
    ("what encryption is for", 30, ("secret", "scramble", "read", "private", "protect",
     "key"), ("aes", "cipher block", "asymmetric"),
     "It scrambles a message so only the person with the key can read it."),
    ("what a backup is for", 30, ("copy", "lose", "lost", "restore", "recover", "break"),
     ("incremental", "raid", "snapshot"),
     "It is a spare copy of your files, so nothing is lost if the original breaks."),
    ("what a web browser does", 30, ("page", "web", "site", "internet", "show", "display"),
     ("rendering engine", "dom", "javascript engine"),
     "It fetches pages from the internet and shows them to you on screen."),
    ("what a password manager is for", 30,
     ("password", "remember", "store", "safe", "different"),
     ("zero-knowledge", "vault encryption", "kdf"),
     "It remembers a different strong password for every site so you do not have to."),
    ("what a search engine does", 30, ("search", "find", "page", "web", "index", "look"),
     ("crawler", "pagerank", "inverted index"),
     "It looks through pages on the web and lists the ones that match what you asked."),
]

_CONSTRAINTS = [
    ("a fruit that is red and grows on a tree", "one word",
     ("apple", "cherry", "cherries", "pomegranate", "plum", "peach", "nectarine",
      "lychee", "rambutan", "mulberry", "persimmon", "crabapple", "guava", "apricot"),
     "Apple"),
    ("an animal that lays eggs and has fur", "one word",
     ("platypus", "echidna"), "Platypus"),
    ("a country in Europe whose name starts with the letter P", "one word",
     ("portugal", "poland"), "Portugal"),
    ("a metal that is lighter than water", "one word",
     ("lithium", "sodium", "potassium"), "Lithium"),
    ("a bird that cannot fly", "one word",
     ("penguin", "ostrich", "emu", "kiwi", "cassowary", "rhea"), "Penguin"),
    ("a planet with no moons", "one word",
     ("mercury", "venus"), "Venus"),
    ("a mammal that can fly", "one word", ("bat", "bats"), "Bat"),
    ("a gas that is lighter than air", "one word",
     ("helium", "hydrogen", "methane", "neon"), "Helium"),
    ("a musical instrument with black and white keys", "one word",
     ("piano", "keyboard", "harpsichord", "organ"), "Piano"),
    ("a colour on the flag of Japan", "one word", ("red", "white"), "Red"),
    ("a sea creature with eight arms", "one word", ("octopus",), "Octopus"),
    ("a month with exactly 30 days", "one word",
     ("april", "june", "september", "november"), "April"),
]


def _chat() -> Iterator[Task]:
    for index, (country, capital) in enumerate(_CAPITALS):
        yield _word_task(
            id=f"chat.capital.{index}",
            use_case="chat",
            label=f"Capital of {country}",
            prompt=(
                f"What is the capital city of {country}? Reply with only the city name."
            ),
            expected=(capital.lower(),),
            rubric=f"{capital}.",
            max_tokens=150,
        )

    for index, (event, year) in enumerate(_EVENTS):
        yield _number_task(
            id=f"chat.year.{index}",
            use_case="chat",
            label="A date from memory",
            prompt=f"In what year did {event}?",
            answer=year,
            rubric=f"{year}.",
            max_tokens=150,
            terse_words=4,
        )

    for index, (what, answers) in enumerate(_FACTS):
        yield _word_task(
            id=f"chat.fact.{index}",
            use_case="chat",
            label="A fact worth knowing",
            prompt=f"What is {what}? Reply with only the answer.",
            expected=answers,
            rubric=f"{answers[0]}.",
            canonical=answers[0],
            max_tokens=200,
            terse_words=6,
        )

    for index, (celsius, ) in enumerate(
        [(0,), (37,), (100,), (25,), (-40,), (18,), (10,), (30,), (-5,), (55,),
         (12,), (45,)]
    ):
        fahrenheit = celsius * 9 / 5 + 32
        yield _number_task(
            id=f"chat.convert.{index}",
            use_case="chat",
            label=f"{celsius}C in Fahrenheit",
            prompt=(
                f"Convert {celsius} degrees Celsius to Fahrenheit, rounded to one decimal "
                "place."
            ),
            answer=round(fahrenheit, 1),
            tolerance=0.2,
            rubric=f"{fahrenheit:g}.",
            max_tokens=300,
        )

    for index, question in enumerate(_UNKNOWABLE):
        yield Task(
            id=f"chat.unknowable.{index}",
            use_case="chat",
            label="Admitting it does not know",
            prompt=f"{question} If you cannot know, say exactly: I don't know.",
            system=TERSE_SYSTEM,
            max_tokens=150,
            rubric=(
                "Any clear admission that it cannot know. What is tested is that it does "
                "not invent an answer."
            ),
            canonical="I don't know.",
            checks=(
                Contains(
                    label="Admitted it cannot know",
                    needles=("i don't know", "i dont know", "i do not know", "cannot know",
                             "can't know", "no way to know", "no way of knowing",
                             "don't have access", "do not have access", "unable to know",
                             "cannot tell", "can't tell", "i have no idea"),
                    any_of=True, weight=3.0),
                Terse(label="Did not pad the answer", max_words=20),
            ),
        )

    for index, (topic, limit, about, jargon, shown) in enumerate(_EXPLAIN):
        yield Task(
            id=f"chat.explain.{index}",
            use_case="chat",
            label=f"Explaining {topic} simply",
            prompt=(
                f"Explain {topic} to someone who has never programmed, in at most {limit} "
                "words. Reply with only the explanation."
            ),
            system=TERSE_SYSTEM,
            max_tokens=200,
            rubric=f"{limit} words or fewer, reaching for the right idea, without jargon.",
            canonical=shown,
            checks=(
                AtMostWords(label=f"Within {limit} words", limit=limit, weight=2.0),
                Contains(label="Reached for the right idea", needles=about, any_of=True,
                         weight=2.0),
                Excludes(label="No jargon left in", needles=jargon),
            ),
        )

    for index, (what, _shape, answers, shown) in enumerate(_CONSTRAINTS):
        yield _word_task(
            id=f"chat.constraint.{index}",
            use_case="chat",
            label="Holding two constraints at once",
            prompt=f"Name {what}. Reply with only the answer, one word.",
            expected=answers,
            rubric=f"Any correct answer, for example {shown}.",
            canonical=shown,
            max_tokens=200,
            terse_words=3,
        )


_BUILDERS = {
    "reasoning": _reasoning,
    "coding": _coding,
    "structured": _structured_all,
    "writing": _writing,
    "chat": _chat,
}


def build(use_case: str) -> list[Task]:
    """Every generated question for a pack, in a stable order.

    Stable because a question's id is how a stored result names what was asked. The
    order here never changes between runs or restarts; what varies is which of them a
    given run samples.
    """

    builder = _BUILDERS.get(use_case)
    return list(builder()) if builder else []
