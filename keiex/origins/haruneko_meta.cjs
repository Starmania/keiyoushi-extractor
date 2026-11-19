// Reads haruneko connector sources with the TypeScript compiler API and prints their metadata.
// stdin: JSON lines {blob, path, text}; stdout: JSON lines {blob, meta} or {blob, error}.
// Only static evaluation: literals, constants of the same file and constructor parameter defaults.
const ts = require(process.env.KEIEX_TYPESCRIPT || 'typescript');
const readline = require('readline');

const LANGUAGES = {
    Multilingual: 'all', Arabic: 'ar', Chinese: 'zh', English: 'en', French: 'fr', German: 'de', Indonesian: 'id',
    Italian: 'it', Japanese: 'ja', Korean: 'ko', Polish: 'pl', Portuguese: 'pt', Russian: 'ru', Spanish: 'es',
    Thai: 'th', Turkish: 'tr', Vietnamese: 'vi',
};
const RATINGS = { Safe: 'SAFE', Suggestive: 'MIXED', Erotica: 'NSFW', Pornographic: 'NSFW' };
const UNRESOLVED = Symbol('unresolved');

function evaluate(node, scope) {
    if (!node) return UNRESOLVED;
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
    if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)) return evaluate(node.expression, scope);
    if (ts.isTemplateExpression(node)) {
        let out = node.head.text;
        for (const span of node.templateSpans) {
            const v = evaluate(span.expression, scope);
            if (v === UNRESOLVED) return UNRESOLVED;
            out += v + span.literal.text;
        }
        return out;
    }
    if (ts.isIdentifier(node)) return scope.has(node.text) ? evaluate(scope.get(node.text), scope) : UNRESOLVED;
    if (ts.isNewExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === 'URL') {
        const [href, base] = (node.arguments || []).map(a => evaluate(a, scope));
        try { return new URL(href, typeof base === 'string' ? base : undefined); } catch { return UNRESOLVED; }
    }
    if (ts.isPropertyAccessExpression(node) && ['origin', 'href', 'hostname'].includes(node.name.text)) {
        const target = evaluate(node.expression, scope);
        if (target instanceof URL) return target[node.name.text];
    }
    if (ts.isPropertyAccessExpression(node)) {
        // Tags.Language.English -> "Language.English"
        const parts = [];
        let n = node;
        while (ts.isPropertyAccessExpression(n)) { parts.unshift(n.name.text); n = n.expression; }
        return ts.isIdentifier(n) && n.text === 'Tags' ? { tag: parts.join('.') } : UNRESOLVED;
    }
    if (ts.isArrayLiteralExpression(node)) {
        const out = [];
        for (const el of node.elements) {
            const v = ts.isSpreadElement(el) ? evaluate(el.expression, scope) : evaluate(el, scope);
            if (v === UNRESOLVED) continue;
            if (ts.isSpreadElement(el) && Array.isArray(v)) out.push(...v); else out.push(v);
        }
        return out;
    }
    return UNRESOLVED;
}

function decoratorsOf(node) {
    return (ts.canHaveDecorators(node) ? ts.getDecorators(node) : undefined) || [];
}

function analyze(path, text) {
    const file = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
    if (path.includes('/templates/') || path.includes('/decorators/')) return { kind: 'theme' };

    const imports = new Map(); // local name -> module specifier
    let icon = null;           // icons can be shared: `import icon from './OtherSite.webp'`
    const scope = new Map();   // file-level constants
    let cls;
    for (const st of file.statements) {
        if (ts.isImportDeclaration(st) && st.importClause) {
            const from = st.moduleSpecifier.text;
            const c = st.importClause;
            if (c.name) imports.set(c.name.text, from);
            if (c.name && from.endsWith('.webp')) icon = from;
            if (c.namedBindings && ts.isNamespaceImport(c.namedBindings)) imports.set(c.namedBindings.name.text, from);
            if (c.namedBindings && ts.isNamedImports(c.namedBindings)) {
                for (const el of c.namedBindings.elements) imports.set(el.name.text, from);
            }
        } else if (ts.isVariableStatement(st)) {
            for (const d of st.declarationList.declarations) {
                if (ts.isIdentifier(d.name) && d.initializer) scope.set(d.name.text, d.initializer);
            }
        } else if (ts.isClassDeclaration(st) && st.modifiers?.some(m => m.kind === ts.SyntaxKind.DefaultKeyword)) {
            cls = st;
        }
    }
    if (!cls) return null;

    const base = cls.heritageClauses?.flatMap(h => h.types)[0]?.expression;
    const baseName = base && ts.isIdentifier(base) ? base.text : base && ts.isPropertyAccessExpression(base) ? base.expression.getText(file) : null;
    const theme = themeOf(baseName, imports, cls);

    const ctor = cls.members.find(ts.isConstructorDeclaration);
    let args = [];
    if (ctor) {
        const ctorScope = new Map(scope);
        for (const p of ctor.parameters) if (ts.isIdentifier(p.name) && p.initializer) ctorScope.set(p.name.text, p.initializer);
        ts.forEachChild(ctor.body, function find(n) {
            if (ts.isCallExpression(n) && n.expression.kind === ts.SyntaxKind.SuperKeyword) {
                args = n.arguments.flatMap(a => {
                    const v = evaluate(ts.isSpreadElement(a) ? a.expression : a, ctorScope);
                    return ts.isSpreadElement(a) && Array.isArray(v) ? v : [v];
                });
            } else ts.forEachChild(n, find);
        });
    }
    const [id, title, url, ...tags] = args;
    const tagNames = tags.filter(t => t && t !== UNRESOLVED && t.tag).map(t => t.tag);
    const languages = tagNames.filter(t => t.startsWith('Language.')).map(t => LANGUAGES[t.slice(9)] || t.slice(9).toLowerCase());
    const rating = tagNames.find(t => t.startsWith('Rating.'));
    const lang = languages.length === 1 ? languages[0] : languages.length ? 'all' : null;
    const str = v => (typeof v === 'string' ? v : null);

    return {
        kind: 'src',
        name: str(title),
        theme,
        icon,
        contentWarning: rating ? RATINGS[rating.slice(7)] || null : null,
        tags: tagNames,
        languages,
        sources: str(id) ? [{
            id: str(id),
            name: str(title) || str(id),
            lang: lang || 'all',
            baseUrl: { type: 'static', defaultUrl: str(url) || '' },
        }] : [],
    };
}

// The shared code a connector builds on: a template it extends or imports, another connector it extends,
// else the decorator module most of its decorators come from (Common is the general toolkit, not a theme).
function themeOf(baseName, imports, cls) {
    const local = from => from.startsWith('./') && !from.startsWith('./decorators/') && !from.startsWith('./templates/') ? from.slice(2) : null;
    if (baseName && imports.has(baseName)) {
        const from = imports.get(baseName);
        if (from.startsWith('./templates/')) return from.slice(2);
        if (local(from)) return local(from);
    }
    for (const from of imports.values()) if (from.startsWith('./templates/')) return from.slice(2);
    const counts = new Map();
    for (const d of decoratorsOf(cls)) {
        const call = d.expression;
        const target = ts.isCallExpression(call) ? call.expression : call;
        if (ts.isPropertyAccessExpression(target) && ts.isIdentifier(target.expression)) {
            const from = imports.get(target.expression.text);
            if (from && from.startsWith('./decorators/') && from !== './decorators/Common') {
                counts.set(from.slice(2), (counts.get(from.slice(2)) || 0) + 1);
            }
        }
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] || null;
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on('line', line => {
    if (!line) return;
    const { blob, path, text } = JSON.parse(line);
    try {
        const meta = analyze(path, text);
        process.stdout.write(JSON.stringify(meta ? { blob, meta } : { blob, error: 'no default export class' }) + '\n');
    } catch (e) {
        process.stdout.write(JSON.stringify({ blob, error: String(e && e.stack || e).slice(0, 2000) }) + '\n');
    }
});
