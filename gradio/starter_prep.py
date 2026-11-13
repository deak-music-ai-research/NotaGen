"""Turn an uploaded score into a NotaGen-format starter (interleaved, rest-omitted,
original key), using the same abctoolkit steps as NotaGen's data/2_data_preprocess.py.

For MusicXML, run xml2abc with the repo's flags first:
    python xml2abc.py -d 8 -c 6 -x -o <tmp_dir> <file.xml>
"""
import re
from abctoolkit.utils import (
    remove_information_field,
    remove_bar_no_annotations,
    Quote_re,
    Barlines,
    extract_metadata_and_parts,
    extract_global_and_local_metadata,
    extract_barline_and_bartext_dict,
)
from abctoolkit.convert import unidecode_abc_lines
from abctoolkit.check import check_alignment_unrotated
from abctoolkit.transpose import Key2index, transpose_an_abc_text

_INLINE_VOICE = re.compile(r'^(\[r:[^\]]*\])?\[V:[^\]]*\]\S')


def is_notagen_format(abc_text):
    """True if the text already has music right after the voice tags, e.g. [V:1]z2|[V:2]x2|"""
    return any(_INLINE_VOICE.match(l.strip()) for l in abc_text.splitlines() if l.strip())


def standard_abc_to_reduced(abc_text):
    abc_lines = [l + '\n' for l in abc_text.splitlines()]
    abc_lines = [l for l in abc_lines if l.strip() != '']

    abc_lines = unidecode_abc_lines(abc_lines)
    abc_lines = remove_information_field(
        abc_lines=abc_lines, info_fields=['X:', 'T:', 'C:', 'W:', 'w:', 'Z:', '%%MIDI'])
    abc_lines = remove_bar_no_annotations(abc_lines)

    for i, line in enumerate(abc_lines):
        if re.search(r'^[A-Za-z]:', line) or line.startswith('%'):
            continue
        if r'\"' in line:
            abc_lines[i] = abc_lines[i].replace(r'\"', '')

    for i, line in enumerate(abc_lines):
        for quote_content in re.findall(Quote_re, line):
            for barline in Barlines:
                if barline in quote_content:
                    line = line.replace(quote_content, '')
                    abc_lines[i] = line

    try:
        _, bar_no_equal_flag, _ = check_alignment_unrotated(abc_lines)
    except Exception:
        bar_no_equal_flag = False
    if not bar_no_equal_flag:
        raise ValueError("The score's voices do not have the same number of bars, "
                         "so it cannot be converted to NotaGen format.")

    for i, line in enumerate(abc_lines):
        for match in re.findall(r'"[^"]*"', line):
            if match == '""':
                line = line.replace(match, '')
            if match[1] in ['^', '_']:
                sub_string = re.sub(r'([^a-zA-Z0-9])\1+', r'\1', match)
                line = line.replace(match, sub_string if len(sub_string) <= 40 else '')
        abc_lines[i] = line

    metadata_lines, _ = extract_metadata_and_parts(abc_lines)
    global_metadata_dict, _ = extract_global_and_local_metadata(metadata_lines)
    ori_key = global_metadata_dict['K'][0]
    if ori_key == 'none':
        ori_key = 'C'

    # keep the piece in its own key (the training script writes 15 transposed copies)
    if ori_key in Key2index:
        text = transpose_an_abc_text(abc_lines, ori_key)
    else:
        text = ''.join(abc_lines)
    lines = [l + '\n' for l in text.split('\n') if l]

    metadata_lines, prefix_dict, left_bl, bar_text, right_bl = extract_barline_and_bartext_dict(lines)
    reduced = list(metadata_lines)
    for i in range(len(bar_text['V:1'])):
        line = ''
        for symbol in prefix_dict.keys():
            if any(c.isalpha() and c not in ('Z', 'z', 'X', 'x') for c in bar_text[symbol][i]):
                if i == 0:
                    line += ('[' + symbol + ']' + prefix_dict[symbol] + left_bl[symbol][0]
                             + bar_text[symbol][0] + right_bl[symbol][0])
                else:
                    line += '[' + symbol + ']' + bar_text[symbol][i] + right_bl[symbol][i]
        reduced.append(line + '\n')
    return ''.join(reduced)


def make_starter(abc_text):
    """Entry point: pass standard ABC (e.g. from xml2abc) or already-NotaGen-format ABC."""
    if is_notagen_format(abc_text):
        # already in NotaGen format: just make sure it is plain ASCII
        return ''.join(unidecode_abc_lines([l + '\n' for l in abc_text.splitlines()]))
    return standard_abc_to_reduced(abc_text)
