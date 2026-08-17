import ast, os

SUSPICIOUS_NAMES = {'first_name', 'last_name', 'full_name', 'username',
                    'title', 'description', 'display', 'group_name', 'msg_text'}

issues = []
for root, dirs, files in os.walk('renewise'):
    if '__pycache__' in root:
        continue
    for f in files:
        if not f.endswith('.py'):
            continue
        path = os.path.join(root, f)
        with open(path, 'r', encoding='utf-8') as fh:
            try:
                src = fh.read()
            except Exception:
                continue
        try:
            tree = ast.parse(src, path)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            fname = func.attr if isinstance(func, ast.Attribute) else ''
            if fname not in ('send_message', 'reply_text', 'edit_message_text', 'send_photo'):
                continue
            has_html = any(
                kw.arg == 'parse_mode'
                and isinstance(kw.value, ast.Constant)
                and kw.value.value == 'HTML'
                for kw in node.keywords
            )
            if not has_html:
                continue
            for arg in (list(node.args)
                        + [kw.value for kw in node.keywords if kw.arg in ('text', 'caption')]):
                if not isinstance(arg, ast.JoinedStr):
                    continue
                for part in ast.walk(arg):
                    if not isinstance(part, ast.FormattedValue):
                        continue
                    val = part.value
                    if isinstance(val, ast.Name) and val.id in SUSPICIOUS_NAMES:
                        issues.append(f'{path}:{node.lineno}: unescaped name {val.id!r}')
                    elif isinstance(val, ast.Attribute) and val.attr in SUSPICIOUS_NAMES:
                        issues.append(f'{path}:{node.lineno}: unescaped attr .{val.attr}')

for i in issues:
    print('ISSUE:', i)
if not issues:
    print('CLEAN: no unescaped user-supplied names in HTML messages')
