"""
Search engine that reads the file-based word index.

Scoring layers (applied in order):

1. Base score per word:
       (frequency × 10) + 1000 [exact-match bonus] − (depth × 5)

2. Coverage bonus (multi-word queries):
       +500 for every query word beyond the first found on the same URL

3. Phrase bonus (words appear consecutively on the page):
       +5000 × phrase_frequency
   This guarantees that pages containing the exact phrase dominate the
   ranking over pages that merely contain the individual words.
"""

import os
import re
import unicodedata
from collections import defaultdict

STORAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'storage')

EXACT_MATCH_BONUS = 1000
FREQ_WEIGHT = 10
DEPTH_PENALTY = 5
COVERAGE_BONUS = 500        # per additional matched query word
PHRASE_BONUS = 5000          # per occurrence of the exact phrase


def _normalize(text):
    """Normalize text for consistent matching (Turkish İ, Unicode, etc.)."""
    text = unicodedata.normalize('NFKC', text)
    text = text.replace('\u0130', 'i')   # İ → i
    return text.lower()


def _count_phrase_occurrences(positions_by_word, query_words):
    """Count how many times *query_words* appear consecutively.

    positions_by_word: dict  word -> set of int positions
    query_words:       list  e.g. ["new", "york", "city"]

    Returns the number of times the full phrase appears as a
    consecutive sequence in the original page text.
    """
    if len(query_words) < 2:
        return 0

    first = query_words[0]
    if first not in positions_by_word:
        return 0

    count = 0
    for start in positions_by_word[first]:
        ok = True
        for offset, w in enumerate(query_words[1:], 1):
            if (start + offset) not in positions_by_word.get(w, set()):
                ok = False
                break
        if ok:
            count += 1
    return count


def search(query, sort_by='relevance', page=1, per_page=20):
    """Search the file-based index for URLs relevant to *query*.

    Returns (results_list, total_count).
    Each result dict contains:
        relevant_url, origin_url, depth, relevance_score,
        frequency, matched_words, phrase_frequency
    """
    words = re.findall(r'[^\W\d_]{2,}', _normalize(query))
    if not words:
        return [], 0

    # -- pass 1: collect per-word scores and positions per URL -----------

    # key = (url, origin, depth)
    aggregated = defaultdict(lambda: {
        'relevant_url': '',
        'origin_url': '',
        'depth': 0,
        'relevance_score': 0,
        'matched_words': [],
        'frequency': 0,
        'phrase_frequency': 0,
        '_positions': {},          # word -> set(int)  (internal, stripped before return)
    })

    for word in words:
        first_letter = word[0]
        if not first_letter.isalpha():
            continue

        filepath = os.path.join(STORAGE_DIR, f'{first_letter}.data')
        if not os.path.exists(filepath):
            continue

        with open(filepath, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.rstrip('\n')
                if not line:
                    continue
                parts = line.split('\t')
                # support both old (5-field) and new (6-field) format
                if len(parts) < 5:
                    continue

                stored_word = parts[0]
                url = parts[1]
                origin = parts[2]
                depth_s = parts[3]
                freq_s = parts[4]
                pos_str = parts[5] if len(parts) >= 6 else ''

                try:
                    depth = int(depth_s)
                    frequency = int(freq_s)
                except ValueError:
                    continue

                if _normalize(stored_word) != word:
                    continue

                score = (frequency * FREQ_WEIGHT) + EXACT_MATCH_BONUS - (depth * DEPTH_PENALTY)

                key = (url, origin, depth)
                entry = aggregated[key]
                entry['relevant_url'] = url
                entry['origin_url'] = origin
                entry['depth'] = depth
                entry['relevance_score'] += score
                entry['frequency'] += frequency
                if word not in entry['matched_words']:
                    entry['matched_words'].append(word)

                # parse positions
                if pos_str:
                    try:
                        positions = set(int(p) for p in pos_str.split(',') if p)
                    except ValueError:
                        positions = set()
                    if word not in entry['_positions']:
                        entry['_positions'][word] = set()
                    entry['_positions'][word].update(positions)

    results = list(aggregated.values())

    # -- pass 2: multi-word bonuses -------------------------------------
    num_query_words = len(words)

    if num_query_words > 1:
        for entry in results:
            matched = len(entry['matched_words'])

            # coverage bonus
            if matched > 1:
                entry['relevance_score'] += (matched - 1) * COVERAGE_BONUS

            # phrase bonus — check if query words appear consecutively
            if matched == num_query_words and entry['_positions']:
                pf = _count_phrase_occurrences(entry['_positions'], words)
                if pf > 0:
                    entry['phrase_frequency'] = pf
                    entry['relevance_score'] += pf * PHRASE_BONUS

    # strip internal fields
    for entry in results:
        entry.pop('_positions', None)

    # -- sort & paginate ------------------------------------------------
    if sort_by == 'relevance':
        results.sort(key=lambda r: r['relevance_score'], reverse=True)
    elif sort_by == 'depth':
        results.sort(key=lambda r: r['depth'])
    elif sort_by == 'frequency':
        results.sort(key=lambda r: r['frequency'], reverse=True)

    total = len(results)
    start = (page - 1) * per_page
    return results[start:start + per_page], total
