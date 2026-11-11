import { Language, defineLanguageFacet, languageDataProp, syntaxHighlighting, syntaxTree } from "@codemirror/language";
import type { EditorState } from "@codemirror/state";
import { NodeProp, NodeSet, NodeType, Parser, Tree, type Input, type PartialParse, type TreeFragment } from "@lezer/common";
import { Tag, styleTags, tagHighlighter } from "@lezer/highlight";
import { HIGHLIGHT_LIMIT, tokenize, type Token, type TokenKind } from "../sqlHighlight";

type TokenInfo = Pick<Token, "kind" | "closed">;
export const tokenInfo = new NodeProp<TokenInfo>();
const data = defineLanguageFacet({
  closeBrackets: { brackets: ["(", "[", "{", "'", '"', "`"] },
});
const kinds: TokenKind[] = ["keyword", "string", "number", "comment", "ident", "variable", "param", "bracket", "punct"];
const tags = Object.fromEntries(kinds.map(kind => [kind, Tag.define()])) as Record<TokenKind, Tag>;
const nodes = [NodeType.define({ id: 0, name: "SQL", top: true, props: [[languageDataProp, data]] })];
const ids = new Map<string, number>();
const styles: Record<string, Tag> = {};
for (const kind of kinds) {
  const quoted = kind === "string" || kind === "comment" || kind === "ident";
  for (const closed of quoted ? [true, false] : [undefined]) {
    const name = closed === false ? `${kind}Unclosed` : kind;
    const info: TokenInfo = closed === undefined ? { kind } : { kind, closed };
    ids.set(name, nodes.length);
    nodes.push(NodeType.define({ id: nodes.length, name, props: [[tokenInfo, info]] }));
    styles[name] = tags[kind];
  }
}
const nodeSet = new NodeSet(nodes).extend(styleTags(styles));

/** One bounded full pass, including long lines. Reuse only wholly unchanged trees. */
class SqlParser extends Parser {
  createParse(input: Input, fragments: readonly TreeFragment[]): PartialParse {
    let parsedPos = 0;
    let tree: Tree | null = fragments.find(fragment => fragment.from === 0 && fragment.to === input.length &&
      fragment.offset === 0 && !fragment.openStart && !fragment.openEnd && fragment.tree.length === input.length &&
      fragment.tree.type === nodeSet.types[0])?.tree ?? null;
    return {
      get parsedPos() { return parsedPos; },
      // This atomic parser always finishes the full input. Reporting a smaller
      // stoppedAt makes the language scheduler repeatedly parse viewport growth.
      get stoppedAt() { return null; },
      stopAt() {},
      advance() {
        parsedPos = input.length;
        if (tree) return tree;
        const buffer: number[] = [];
        if (input.length <= HIGHLIGHT_LIMIT) {
          for (const token of tokenize(input.read(0, input.length))) {
            const name = token.closed === false ? `${token.kind}Unclosed` : token.kind;
            buffer.push(ids.get(name)!, token.start, token.end, 4);
          }
        }
        tree = Tree.build({ buffer, nodeSet, topID: 0, length: input.length });
        return tree;
      },
    };
  }
}

export const sqlLanguage = new Language(data, new SqlParser(), [], "SQL");
export const sqlHighlighting = syntaxHighlighting(tagHighlighter(
  kinds.map(kind => ({ tag: tags[kind], class: `tok-${kind}` })),
));

/** Read the existing tree, without running the tokenizer or mutating state. */
export function treeTokens(state: EditorState): Token[] {
  const tokens: Token[] = [];
  syntaxTree(state).iterate({ enter(node) {
    const info = node.type.prop(tokenInfo);
    if (info) tokens.push({ ...info, start: node.from, end: node.to });
  } });
  return tokens;
}
