"""Bounded trigger intervention; never rewrite or evaluate an oracle."""
import ast

def oracle_nodes(tree):
    result=[]
    for n in ast.walk(tree):
        if isinstance(n,ast.Assert): result.append(n)
        elif isinstance(n,ast.Call):
            name=n.func.attr if isinstance(n.func,ast.Attribute) else getattr(n.func,'id','')
            if name.startswith('assert') or name in {'raises','warns','fail'}: result.append(n)
    return result

def make_control(code, proposal):
    if proposal.get('abstain'): raise ValueError('MODEL_ABSTAIN')
    tree=ast.parse(code); line=proposal['line']; before=proposal['before']; after=proposal['after']
    matches=[n for n in ast.walk(tree) if isinstance(n,ast.expr) and n.lineno==line and ast.get_source_segment(code,n)==before]
    if len(matches)!=1: raise ValueError('NON_UNIQUE_EXPRESSION')
    target=matches[0]
    if target.lineno!=target.end_lineno: raise ValueError('MULTILINE_EDIT')
    protected=[]
    for n in ast.walk(tree):
        if isinstance(n,(ast.Assert,ast.Import,ast.ImportFrom,ast.With,ast.Try)): protected.append(n)
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)): protected.extend(n.decorator_list)
    protected+=oracle_nodes(tree)
    protected += [n.func for n in ast.walk(tree) if isinstance(n, ast.Call)]
    if any(target in list(ast.walk(n)) for n in protected): raise ValueError('PROTECTED_ORACLE_OR_PROTOCOL')
    # Small input expressions only: literals, containers, names, unary operators.
    allowed=(ast.Expression,ast.Constant,ast.Name,ast.Load,ast.Tuple,ast.List,ast.Set,ast.Dict,ast.UnaryOp,ast.USub,ast.UAdd,ast.Not)
    replacement=ast.parse(after,mode='eval')
    if any(not isinstance(n,allowed) for n in ast.walk(replacement)): raise ValueError('UNSUPPORTED_REPLACEMENT')
    if any(isinstance(n,(ast.Call,ast.Attribute,ast.Subscript)) for n in ast.walk(target)): raise ValueError('CALL_OR_OBSERVATION_EDIT')
    lines=code.splitlines(keepends=True); raw=lines[line-1].encode()
    lines[line-1]=(raw[:target.col_offset]+after.encode()+raw[target.end_col_offset:]).decode()
    changed=''.join(lines); other=ast.parse(changed)
    orig_oracles=[ast.get_source_segment(code,n) for n in oracle_nodes(tree)]
    new_oracles=[ast.get_source_segment(changed,n) for n in oracle_nodes(other)]
    if orig_oracles!=new_oracles: raise ValueError('ORACLE_CHANGED')
    if code==changed: raise ValueError('NO_CHANGE')
    return changed

TRACE_SOURCE='''import sys, atexit, json
seen = set()
def trace(frame, event, arg):
    if event == "call":
        name = frame.f_code.co_filename.replace("\\\\", "/")
        if name.endswith(TARGET_FILE) and frame.f_code.co_name == TARGET_FUNCTION:
            seen.add((TARGET_FILE, TARGET_FUNCTION))
    return trace
sys.setprofile(trace)
atexit.register(lambda: sys.stderr.write("\\nBRT_CONTRAST_REACH=" + json.dumps(sorted(seen)) + "\\n"))
'''
