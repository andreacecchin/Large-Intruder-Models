"""
INTRUDER — an LLM social deduction game
7 models, 3 rounds, one secret impostor.
"""

import datetime
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from openai import OpenAI
from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

# ─── CONFIG ───────────────────────────────────────────────────────────────────

MODELS = [
    {"id": "kimi-k2-thinking:cloud",        "name": "KimiK2",            "company": "MoonlightAI",   "color": "blue"},
    {"id": "gpt-oss:120b-cloud",            "name": "GPToss-120B",       "company": "OpenAI",        "color": "green"},
    {"id": "nemotron-3-super:cloud",        "name": "Nemotron3-120B",    "company": "NVIDIA",        "color": "red"},
    {"id": "deepseek-v3.1:671b-cloud",      "name": "DeepSeek3.1",       "company": "DeepSeek",      "color": "bright_magenta"},
    {"id": "qwen3-next:80b-cloud",          "name": "Qwen3-80B",         "company": "Alibaba",       "color": "brown"},
    {"id": "ministral-3:14b-cloud",         "name": "Ministral3-14B",    "company": "MistralAI",     "color": "yellow"},
    {"id": "gemma3:27b-cloud",              "name": "Gemma3-27B",        "company": "Google",        "color": "magenta"},
]

OLLAMA_URL  = "http://localhost:11434/v1"
MAX_ROUNDS  = 3

# ─── LOGGER ───────────────────────────────────────────────────────────────────

LOG_FILE = Path("log.txt")

def log_init(majority_word: str, intruder_word: str):
    """Create/overwrite log file with game header."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write(f"INTRUDER — Game log\n")
        f.write(f"Started: {ts}\n")
        f.write(f"Majority word : {majority_word}\n")
        f.write(f"Intruder word : {intruder_word}\n")
        f.write("=" * 80 + "\n\n")

def log_call(
    phase:      str,   # "HINT" | "VOTE"
    round_num:  int,
    player:     "Player",
    system:     str,
    user:       str,
    raw:        str,
    parsed:     str,
):
    """Append a single API call — prompt in, raw response out, parsed result."""
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{'─' * 80}\n")
        f.write(f"[Round {round_num}] [{phase}] {player.name} ({player.id})\n")
        f.write(f"{'─' * 80}\n")
        f.write("SYSTEM PROMPT:\n")
        f.write(system + "\n\n")
        f.write("USER PROMPT:\n")
        f.write(user + "\n\n")
        f.write("RAW RESPONSE:\n")
        f.write(raw + "\n\n")
        f.write(f"PARSED → {parsed}\n")
        f.write("\n")

def log_section(title: str):
    """Write a visible section separator to the log."""
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n{'#' * 80}\n")
        f.write(f"# {title}\n")
        f.write(f"{'#' * 80}\n\n")

def log_players(players: list["Player"]):
    """Log the player/intruder assignment at game start."""
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write("PLAYER ASSIGNMENTS:\n")
        for p in players:
            role = "INTRUDER" if p.is_intruder else "group  "
            f.write(f"  {role}  {p.name:14} ({p.id})  word={p.secret_word}\n")
        f.write("\n")

# ─── DATA STRUCTURES ──────────────────────────────────────────────────────────

@dataclass
class Player:
    id:       str        # ollama model id
    name:     str
    company:  str
    color:    str
    alive:    bool = True
    is_intruder: bool = False
    secret_word: str = ""

@dataclass
class HintEntry:
    player_name: str
    word:        str
    round_num:   int

@dataclass
class VoteEntry:
    voter:     str
    voted_for: str
    reason:    str
    round_num: int

@dataclass
class GameState:
    players:        list[Player]
    majority_word:  str = ""
    intruder_word:  str = ""
    hints:          list[HintEntry] = field(default_factory=list)
    votes:          list[VoteEntry] = field(default_factory=list)
    eliminated:     list[str]       = field(default_factory=list)  # player names
    current_round:  int = 1
    game_over:      bool = False
    winner:         str = ""  # "intruder" | "group"

# ─── OLLAMA CLIENT ────────────────────────────────────────────────────────────

client = OpenAI(base_url=OLLAMA_URL, api_key="ollama")

def call_model(model_id: str, system: str, user: str, temperature: float = 0.8) -> str:
    """Call a model via Ollama OpenAI-compatible API. Returns raw response text."""
    try:
        resp = client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            temperature=temperature,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"[ERROR: {e}]"

# ─── PROMPT BUILDERS ──────────────────────────────────────────────────────────

def build_hint_system(player: Player) -> str:
    return (
        "You are playing a word deduction game called Intruder. "
        "There are two secret words in play: one given to most players (the majority word), "
        "and a different word given to exactly one player (the intruder). "
        "You do NOT know which word (the majority or the intruder ones) you received, so you don't know if you are the intruder. "
        "Your goal: say ONE single word that is clearly associated with YOUR secret word. "
        "IF you suspect you are the intruder, you have to try to blend in by hinting toward "
        "what you think the majority word might be, saying a word which is associable to what you think is others' majority word, hiding your intruder identity.  "
        "Respond in EXACTLY this format:\n"
        "REASONING: <free space to create your small and brief reasoning, to figure out based on others hints (when available) if you may be the intruder>\n"
        "HINT: <your hint>\n"
        "Rules: your hint MUST BE ONLY one word, you CANNOT use word already said by other players (also in prevoius rounds) NEITHER the secret word, no punctuation, no explanation."
    )

def build_hint_user(player: Player, state: GameState) -> str:
    lines = [f"Your secret word is: {player.secret_word}"]

    # History from previous rounds
    prev_rounds = [h for h in state.hints if h.round_num < state.current_round]
    if prev_rounds:
        lines.append("\n--- Previous rounds ---")
        for r in range(1, state.current_round):
            round_hints = [h for h in prev_rounds if h.round_num == r]
            if round_hints:
                lines.append(f"Round {r} hints:")
                for h in round_hints:
                    lines.append(f"  {h.player_name}: {h.word}")
        # Also show who was eliminated after each round
        elim_so_far = state.eliminated[: state.current_round - 1]
        if elim_so_far:
            lines.append(f"Eliminated so far: {', '.join(elim_so_far)}")

    # Hints from THIS round already given (players before this one)
    current_hints = [h for h in state.hints if h.round_num == state.current_round]
    if current_hints:
        lines.append(f"\n--- Round {state.current_round} hints so far ---")
        for h in current_hints:
            lines.append(f"  {h.player_name}: {h.word}")

    lines.append("\nNow give your single hint word:")
    return "\n".join(lines)


def build_vote_system(player: Player) -> str:
    return (
        "You are playing a word deduction game called Intruder, the rule are:\n "
        "There are two secret words in play: one given to most players (the majority word), "
        "and a different word given to exactly one player (the intruder). "
        "You do NOT know which word (the majority or the intruder ones) you received, so you don't know if you are the intruder. "
        "Your goal: say ONE single word that is clearly associated with YOUR secret word. "
        "IF you suspect you are the intruder, you have to try to blend in by hinting toward "
        "what you think the majority word might be, saying a word which is associable to what you think is others' majority word, hiding your intruder identity.  "
        "Rules: respond with ONLY one word, you CANNOT use word already said by other players (also in prevoius rounds) NEITHER the secret word, no punctuation, no explanation.\n\n"
        "Based on the secret word you received, comparing it with others' hints, try to identify which player you think is the intruder — "
        "the one whose word is different from the majority. "
        "You cannot vote for yourself, so if you think to be the intruder, vote someone else. "
        "Respond in EXACTLY this format:\n"
        "REASONING: <free space to create your reasoning, one long consecutive sentence>\n"
        "VOTE AS INTRUDER: <player name you think is the intruder>\n"
        "REASON: <one very brief sentence that explains why you think it is the intruder>\n"
        "Nothing else. All players have now given their hint words."
    )

def build_vote_user(player: Player, state: GameState, alive_players: list[Player]) -> str:
    lines = [f"The secret word you received was: {player.secret_word}"]

    # Full history
    for r in range(1, state.current_round + 1):
        round_hints = [h for h in state.hints if h.round_num == r]
        if round_hints:
            lines.append(f"\nRound {r} hints:")
            for h in round_hints:
                lines.append(f"  {h.player_name} said {h.word}")

    lines.append(f"\nEliminated so far: {', '.join(state.eliminated) if state.eliminated else 'none'}")

    votable = [p.name for p in alive_players if p.name != player.name]
    lines.append(f"\nYou must vote for one of: {', '.join(votable)} (the other one is you).")
    lines.append("Remember: REASONING: <small reasoning based on secret word and what other said> then VOTE AS INTRUDER: <name>  then  REASON: <one sentence>")
    return "\n".join(lines)

# ─── PARSE VOTE ───────────────────────────────────────────────────────────────

def parse_vote(raw: str, valid_names: list[str]) -> tuple[str, str]:
    voted = ""
    reason = ""

    for line in raw.splitlines():
        stripped = line.strip()
        upper = stripped.upper()

        # Priorità alla riga di voto esplicita — ignora REASONING
        if upper.startswith("VOTE") and ":" in stripped:
            candidate = stripped.split(":", 1)[1].strip()
            for name in valid_names:
                if name.lower() in candidate.lower():
                    voted = name
                    break

        elif upper.startswith("REASON:") and not upper.startswith("REASONING:"):
            reason = stripped[7:].strip()

    # Fallback: cerca nome valido solo se il voto non è stato trovato
    if not voted:
        for name in valid_names:
            if name.lower() in raw.lower():
                voted = name
                break

    if not voted and valid_names:
        voted = random.choice(valid_names)
    if not reason:
        reason = "(no reason given)"

    return voted, reason

def parse_hint(raw: str) -> str:
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("HINT:"):
            word = stripped[5:].strip()
            word = re.sub(r"[^a-zA-Z'-]", "", word.split()[0]) if word.split() else ""
            return word if word else raw.split()[0]
    
    # Fallback: prima parola della risposta (comportamento precedente)
    word = re.split(r"[\s\n]+", raw.strip())[0]
    return re.sub(r"[^a-zA-Z'-]", "", word)

# ─── RICH CONSOLE HELPERS ─────────────────────────────────────────────────────

console = Console()

def header():
    console.print()
    title = Text("L a r g e  I N T R U D E R  M o d e l s", style="bold white on dark_red", justify="center")
    console.print(Panel(title, border_style="red", padding=(1, 4)))
    console.print()

def player_badge(p: Player) -> Text:
    icon = "☠" if not p.alive else ("👁" if p.is_intruder else "●")  # icon only shown post-reveal
    return Text(f"{icon} {p.name} [{p.company}]", style=p.color)

def print_players_table(players: list[Player], reveal: bool = False):
    t = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold dim")
    t.add_column("Player",  style="bold", min_width=14)
    t.add_column("Company", style="dim",  min_width=10)
    t.add_column("Status",  min_width=10)
    if reveal:
        t.add_column("Role",  min_width=10)
        t.add_column("Word",  min_width=10)
    for p in players:
        status = "[green]Alive[/]" if p.alive else "[red]Eliminated[/]"
        if reveal:
            role = "[red bold]INTRUDER[/]" if p.is_intruder else "[blue]Group[/]"
            word = f"[yellow]{p.secret_word}[/]"
            t.add_row(p.name, p.company, status, role, word)
        else:
            t.add_row(p.name, p.company, status)
    console.print(t)

def print_round_banner(round_num: int):
    console.print()
    console.print(Rule(f"[bold red]  ROUND {round_num} of {MAX_ROUNDS}  ", style="red"))
    console.print()

def print_hint_phase(round_num: int):
    console.print(Panel("[bold]💬  HINT PHASE[/bold]", style="dim blue", padding=(0, 2)))
    console.print()

def print_vote_phase():
    console.print(Panel("[bold]🗳️  VOTE PHASE[/bold]", style="dim yellow", padding=(0, 2)))
    console.print()

def print_hint(player: Player, word: str):
    badge = f"[{player.color}]{player.name:14}[/]"
    console.print(f"  {badge}  →  [bold white]{word}[/]")

def print_vote_result(voter: str, voted_for: str, reason: str, voter_color: str):
    console.print(f"  [{voter_color}]{voter:14}[/]  votes  [bold]{voted_for}[/]")
    console.print(f"  {'':14}     [dim italic]{reason}[/]")
    console.print()

def print_elimination(name: Optional[str], is_intruder: bool = False):
    console.print()
    if name is None:
        console.print(Panel(
            "[yellow bold]⚖  TIE VOTE — no elimination this round.[/]",
            border_style="yellow"
        ))
    elif is_intruder:
        console.print(Panel(
            f"[red bold]☠  {name} has been eliminated — and they were the INTRUDER![/]",
            border_style="red"
        ))
    else:
        console.print(Panel(
            f"[dim]☠  {name} has been eliminated. They were innocent.[/]",
            border_style="dim"
        ))
    console.print()

def print_final_result(winner: str, intruder: Player, majority_word: str, intruder_word: str):
    console.print()
    console.print(Rule(style="bright_red"))
    if winner == "group":
        msg = Text("🏆  THE GROUP WINS!", style="bold green", justify="center")
    else:
        msg = Text("👁  THE INTRUDER WINS!", style="bold red", justify="center")

    details = (
        f"\n[bold]Intruder:[/] [{intruder.color}]{intruder.name}[/] ({intruder.company})\n"
        f"[bold]Majority word:[/] [cyan]{majority_word}[/]\n"
        f"[bold]Intruder word:[/] [yellow]{intruder_word}[/]"
    )
    console.print(Panel(Align(msg, align="center"), border_style="bright_white", padding=(1, 4)))
    console.print(Panel(details, title="Game Summary", border_style="dim"))

# ─── GAME LOGIC ───────────────────────────────────────────────────────────────

def setup_game(majority_word: str, intruder_word: str) -> GameState:
    players = [Player(**{k: v for k, v in m.items()}) for m in MODELS]
    intruder_idx = random.randint(0, len(players) - 1)
    for i, p in enumerate(players):
        p.is_intruder = (i == intruder_idx)
        p.secret_word = intruder_word if p.is_intruder else majority_word
    return GameState(
        players=players,
        majority_word=majority_word,
        intruder_word=intruder_word,
    )

def alive_players(state: GameState) -> list[Player]:
    return [p for p in state.players if p.alive]

def run_hint_phase(state: GameState):
    log_section(f"ROUND {state.current_round} — HINT PHASE")
    print_hint_phase(state.current_round)
    order = alive_players(state)
    random.shuffle(order)

    for player in order:
        sys_prompt  = build_hint_system(player)
        user_prompt = build_hint_user(player, state)
        raw  = call_model(player.id, sys_prompt, user_prompt)
        word = parse_hint(raw)
        log_call("HINT", state.current_round, player, sys_prompt, user_prompt, raw, word)
        state.hints.append(HintEntry(player.name, word, state.current_round))
        print_hint(player, word)
        time.sleep(0.3)

def run_vote_phase(state: GameState) -> Optional[str]:
    """Run voting, return eliminated player name or None on tie."""
    log_section(f"ROUND {state.current_round} — VOTE PHASE")
    print_vote_phase()
    alive = alive_players(state)
    valid_names = [p.name for p in alive]
    vote_tally: dict[str, int] = {name: 0 for name in valid_names}

    for player in alive:
        sys_prompt  = build_vote_system(player)
        user_prompt = build_vote_user(player, state, alive)
        raw = call_model(player.id, sys_prompt, user_prompt, temperature=0.5)

        votable = [n for n in valid_names if n != player.name]
        voted, reason = parse_vote(raw, votable)
        log_call("VOTE", state.current_round, player, sys_prompt, user_prompt, raw, f"voted={voted} | reason={reason}")
        vote_tally[voted] = vote_tally.get(voted, 0) + 1
        state.votes.append(VoteEntry(player.name, voted, reason, state.current_round))
        print_vote_result(player.name, voted, reason, player.color)

    # Tally
    max_votes = max(vote_tally.values())
    top = [name for name, v in vote_tally.items() if v == max_votes]

    console.print(Rule("[dim]Vote tally", style="dim"))
    t = Table(box=box.MINIMAL, show_header=False)
    for name, count in sorted(vote_tally.items(), key=lambda x: -x[1]):
        bar = "█" * count
        t.add_row(f"[bold]{name}[/]", f"[yellow]{bar}[/]", str(count))
    console.print(t)
    console.print()

    if len(top) > 1:
        return None  # tie

    eliminated_name = top[0]
    for p in state.players:
        if p.name == eliminated_name:
            p.alive = False
            state.eliminated.append(p.name)
            is_intruder = p.is_intruder
            print_elimination(eliminated_name, is_intruder)
            if is_intruder:
                state.game_over = True
                state.winner = "group"
            return eliminated_name

    return None

def check_game_over(state: GameState):
    """Check if intruder is isolated (wins) or all rounds done."""
    if state.game_over:
        return
    if state.current_round > MAX_ROUNDS:
        state.game_over = True
        state.winner = "intruder"

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    header()

    console.print("[bold]Welcome to [red]INTRUDER[/red].[/bold]")
    console.print("7 language models will play a word deduction game.")
    console.print("One of them holds a different secret word. Can the group find the impostor?\n")

    majority_word = Prompt.ask("[bold cyan]Enter the MAJORITY word[/bold cyan] (given to 6 players)")
    intruder_word = Prompt.ask("[bold yellow]Enter the INTRUDER word[/bold yellow] (given to 1 player)")

    console.print()
    state = setup_game(majority_word.strip().lower(), intruder_word.strip().lower())

    log_init(majority_word.strip().lower(), intruder_word.strip().lower())
    log_players(state.players)

    console.print("[bold]Players:[/bold]")
    print_players_table(state.players)
    console.print()
    console.print("[dim]The intruder has been assigned secretly. Let the game begin.[/dim]")

    for round_num in range(1, MAX_ROUNDS + 1):
        state.current_round = round_num
        print_round_banner(round_num)

        # Hint phase
        run_hint_phase(state)

        # Vote phase
        run_vote_phase(state)

        # Check if intruder was caught
        if state.game_over and state.winner == "group":
            break

        check_game_over(state)
        if state.game_over:
            break

        # Pause between rounds
        if round_num < MAX_ROUNDS:
            console.print(f"[dim]Advancing to round {round_num + 1}...[/dim]\n")
            time.sleep(0.5)

    # Final result
    intruder = next(p for p in state.players if p.is_intruder)
    print_final_result(state.winner, intruder, majority_word, intruder_word)

    # Final reveal table
    console.print("\n[bold]Final player status:[/bold]")
    print_players_table(state.players, reveal=True)


if __name__ == "__main__":
    main()