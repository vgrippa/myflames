"""Terminal-safe text layout shared by engines and command-line adapters."""
from .connector import terminal_text


def _table(headers, rows):
    """Left-align every column except those whose header starts with '>'."""
    right = [h.startswith('>') for h in headers]
    headers = [h.lstrip('>') for h in headers]
    # Escaped before measuring, so a control character cannot break the columns.
    rows = [[terminal_text(cell) for cell in row] for row in rows]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]

    def line(cells):
        out = []
        for i, cell in enumerate(cells):
            last = i == len(cells) - 1
            if right[i]:
                out.append(cell.rjust(widths[i]))
            else:
                out.append(cell if last else cell.ljust(widths[i]))
        return '  '.join(out).rstrip()
    return '\n'.join([line(headers)] + [line(r) for r in rows]) + '\n'

