"""
mini_javac.py - a tiny Java compiler written in Python.

It compiles a small subset of Java into JVM-style bytecode, the same
instructions real `javac` produces (iload, iadd, invokevirtual, ...).

Pipeline (same stages as the real javac):
    1. Lexer   : source text  -> tokens
    2. Parser  : tokens       -> AST (abstract syntax tree)
    3. CodeGen : AST          -> bytecode, one ClassFile per class

Supported Java subset
    - classes with int / boolean / object fields (static or instance)
    - methods and constructors, int / boolean / object parameters
    - local variables, = += -= *= /= %=, ++ and -- (as statements)
    - + - * / %, < <= > >= == !=, && || !, unary -
    - if / else, while, for, return
    - method calls, `new`, field access, `this`
    - System.out.println(...) / System.out.print(...) with int, boolean or "string"

Simplifications compared with real javac
    - Output is a readable .class.json file instead of the binary .class format.
    - Symbolic references (e.g. "Point.x:I") are stored inline in each
      instruction instead of in a constant pool.
    - No overloading, arrays (except `String[] args`), strings beyond literals,
      or using the value of an assignment inside a bigger expression.

Usage
    python mini_javac.py Main.java          # compile and print javap-style output
    python mini_javac.py Main.java -o out/  # also choose the output folder
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional


class CompileError(Exception):
    def __init__(self, message: str, line: int = 0):
        super().__init__(f"line {line}: {message}" if line else message)


# =====================================================================
# 1. LEXER - turns source text into a list of tokens
# =====================================================================

KEYWORDS = {
    "class", "public", "private", "protected", "static", "final",
    "int", "boolean", "void", "if", "else", "while", "for",
    "return", "new", "true", "false", "this",
}

TOKEN_SPEC = [
    ("WS",      r"[ \t\r\n]+"),
    ("COMMENT", r"//[^\n]*|/\*.*?\*/"),
    ("NUMBER",  r"\d+"),
    ("STRING",  r'"(?:\\.|[^"\\])*"'),
    ("IDENT",   r"[A-Za-z_]\w*"),
    ("OP",      r"==|!=|<=|>=|&&|\|\||\+\+|--|\+=|-=|\*=|/=|%=|[+\-*/%<>=!(){}\[\];,.]"),
]
TOKEN_RE = re.compile("|".join(f"(?P<{n}>{p})" for n, p in TOKEN_SPEC), re.DOTALL)


@dataclass
class Token:
    kind: str    # NUMBER, STRING, IDENT, KEYWORD, OP, EOF
    value: str
    line: int


def tokenize(source: str) -> list[Token]:
    tokens, pos, line = [], 0, 1
    while pos < len(source):
        m = TOKEN_RE.match(source, pos)
        if not m:
            raise CompileError(f"unexpected character {source[pos]!r}", line)
        kind, text = m.lastgroup, m.group()
        if kind == "IDENT" and text in KEYWORDS:
            kind = "KEYWORD"
        if kind not in ("WS", "COMMENT"):
            tokens.append(Token(kind, text, line))
        line += text.count("\n")
        pos = m.end()
    tokens.append(Token("EOF", "", line))
    return tokens


# =====================================================================
# 2. AST - the tree the parser builds
# =====================================================================

@dataclass
class Node:
    line: int = field(default=0, kw_only=True)

# --- expressions ---
@dataclass
class IntLit(Node):     value: int
@dataclass
class BoolLit(Node):    value: bool
@dataclass
class StrLit(Node):     value: str
@dataclass
class Name(Node):       name: str
@dataclass
class This(Node):       pass
@dataclass
class FieldAccess(Node):
    obj: Node
    name: str
@dataclass
class Call(Node):
    obj: Optional[Node]      # None means "a method of this class"
    name: str
    args: list
@dataclass
class New(Node):
    cls: str
    args: list
@dataclass
class Binary(Node):
    op: str
    left: Node
    right: Node
@dataclass
class Unary(Node):
    op: str
    expr: Node
@dataclass
class Assign(Node):
    target: Node             # Name or FieldAccess
    op: str                  # "=", "+=", ...
    value: Node
@dataclass
class IncDec(Node):
    target: Node
    op: str                  # "++" or "--"

# --- statements ---
@dataclass
class VarDecl(Node):
    type: str
    name: str
    init: Optional[Node]
@dataclass
class ExprStmt(Node):   expr: Node
@dataclass
class Block(Node):      stmts: list
@dataclass
class If(Node):
    cond: Node
    then: Node
    else_: Optional[Node]
@dataclass
class While(Node):
    cond: Node
    body: Node
@dataclass
class For(Node):
    init: Optional[Node]
    cond: Optional[Node]
    update: Optional[Node]
    body: Node
@dataclass
class Return(Node):     expr: Optional[Node]

# --- declarations ---
@dataclass
class FieldDecl(Node):
    type: str
    name: str
    static: bool
    init: Optional[Node]
@dataclass
class MethodDecl(Node):
    name: str
    ret: str
    params: list             # [(type, name)]
    static: bool
    body: Block
@dataclass
class ClassDecl(Node):
    name: str
    fields: list
    methods: list


# =====================================================================
# 3. PARSER - recursive descent, tokens -> AST
# =====================================================================

class Parser:
    MODIFIERS = ("public", "private", "protected", "static", "final")

    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.i = 0

    # ---- token helpers ----
    def peek(self, k: int = 0) -> Token:
        return self.tokens[min(self.i + k, len(self.tokens) - 1)]

    def next(self) -> Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def check(self, value: str, k: int = 0) -> bool:
        tok = self.peek(k)
        return tok.kind in ("OP", "KEYWORD") and tok.value == value

    def expect(self, value: str) -> Token:
        if not self.check(value):
            tok = self.peek()
            raise CompileError(f"expected '{value}' but found '{tok.value or 'end of file'}'", tok.line)
        return self.next()

    def expect_ident(self) -> str:
        tok = self.next()
        if tok.kind != "IDENT":
            raise CompileError(f"expected a name but found '{tok.value}'", tok.line)
        return tok.value

    # ---- program structure ----
    def parse_program(self) -> list[ClassDecl]:
        classes = []
        while self.peek().kind != "EOF":
            classes.append(self.parse_class())
        return classes

    def parse_class(self) -> ClassDecl:
        while self.peek().value in self.MODIFIERS:
            self.next()
        line = self.expect("class").line
        cls = ClassDecl(self.expect_ident(), [], [], line=line)
        self.expect("{")
        while not self.check("}"):
            self.parse_member(cls)
        self.expect("}")
        return cls

    def parse_member(self, cls: ClassDecl):
        line = self.peek().line
        static = False
        while self.peek().kind == "KEYWORD" and self.peek().value in self.MODIFIERS:
            if self.next().value == "static":
                static = True

        # constructor:  ClassName(params) { ... }
        if self.peek().value == cls.name and self.check("(", 1):
            self.next()
            params = self.parse_params()
            cls.methods.append(MethodDecl("<init>", "void", params, False, self.parse_block(), line=line))
            return

        typ = self.parse_type()
        name = self.expect_ident()
        if self.check("("):
            params = self.parse_params()
            cls.methods.append(MethodDecl(name, typ, params, static, self.parse_block(), line=line))
        else:
            init = None
            if self.check("="):
                self.next()
                init = self.parse_expr()
            self.expect(";")
            cls.fields.append(FieldDecl(typ, name, static, init, line=line))

    def parse_type(self) -> str:
        tok = self.next()
        if not (tok.kind == "IDENT" or tok.value in ("int", "boolean", "void")):
            raise CompileError(f"expected a type but found '{tok.value}'", tok.line)
        name = tok.value
        while self.check("["):
            self.next()
            self.expect("]")
            name += "[]"
        return name

    def parse_params(self) -> list:
        self.expect("(")
        params = []
        if not self.check(")"):
            while True:
                params.append((self.parse_type(), self.expect_ident()))
                if not self.check(","):
                    break
                self.next()
        self.expect(")")
        return params

    # ---- statements ----
    def parse_block(self) -> Block:
        line = self.expect("{").line
        stmts = []
        while not self.check("}"):
            stmts.append(self.parse_statement())
        self.expect("}")
        return Block(stmts, line=line)

    def is_var_decl(self) -> bool:
        tok = self.peek()
        if tok.kind == "KEYWORD" and tok.value in ("int", "boolean"):
            return True
        if tok.kind == "IDENT":
            if self.peek(1).kind == "IDENT":                      # Point p
                return True
            if self.check("[", 1) and self.check("]", 2):          # String[] s
                return True
        return False

    def parse_var_decl(self) -> VarDecl:
        line = self.peek().line
        typ = self.parse_type()
        name = self.expect_ident()
        init = None
        if self.check("="):
            self.next()
            init = self.parse_expr()
        return VarDecl(typ, name, init, line=line)

    def parse_statement(self) -> Node:
        tok = self.peek()
        line = tok.line
        if self.check("{"):
            return self.parse_block()
        if self.check("if"):
            self.next(); self.expect("(")
            cond = self.parse_expr()
            self.expect(")")
            then = self.parse_statement()
            else_ = None
            if self.check("else"):
                self.next()
                else_ = self.parse_statement()
            return If(cond, then, else_, line=line)
        if self.check("while"):
            self.next(); self.expect("(")
            cond = self.parse_expr()
            self.expect(")")
            return While(cond, self.parse_statement(), line=line)
        if self.check("for"):
            self.next(); self.expect("(")
            init = None
            if not self.check(";"):
                init = self.parse_var_decl() if self.is_var_decl() else ExprStmt(self.parse_expr(), line=line)
            self.expect(";")
            cond = None if self.check(";") else self.parse_expr()
            self.expect(";")
            update = None if self.check(")") else ExprStmt(self.parse_expr(), line=line)
            self.expect(")")
            return For(init, cond, update, self.parse_statement(), line=line)
        if self.check("return"):
            self.next()
            expr = None if self.check(";") else self.parse_expr()
            self.expect(";")
            return Return(expr, line=line)
        if self.is_var_decl():
            decl = self.parse_var_decl()
            self.expect(";")
            return decl
        expr = self.parse_expr()
        self.expect(";")
        return ExprStmt(expr, line=line)

    # ---- expressions (lowest precedence first) ----
    def parse_expr(self) -> Node:
        return self.parse_assign()

    def parse_assign(self) -> Node:
        left = self.parse_binary(0)
        tok = self.peek()
        if tok.kind == "OP" and tok.value in ("=", "+=", "-=", "*=", "/=", "%="):
            self.next()
            if not isinstance(left, (Name, FieldAccess)):
                raise CompileError("left side of assignment must be a variable or field", tok.line)
            return Assign(left, tok.value, self.parse_assign(), line=tok.line)
        return left

    PRECEDENCE = [("||",), ("&&",), ("==", "!="), ("<", "<=", ">", ">="), ("+", "-"), ("*", "/", "%")]

    def parse_binary(self, level: int) -> Node:
        if level == len(self.PRECEDENCE):
            return self.parse_unary()
        left = self.parse_binary(level + 1)
        while self.peek().kind == "OP" and self.peek().value in self.PRECEDENCE[level]:
            tok = self.next()
            left = Binary(tok.value, left, self.parse_binary(level + 1), line=tok.line)
        return left

    def parse_unary(self) -> Node:
        tok = self.peek()
        if tok.kind == "OP" and tok.value in ("!", "-"):
            self.next()
            return Unary(tok.value, self.parse_unary(), line=tok.line)
        if tok.kind == "OP" and tok.value in ("++", "--"):
            self.next()
            return IncDec(self.parse_unary(), tok.value, line=tok.line)
        return self.parse_postfix()

    def parse_postfix(self) -> Node:
        expr = self.parse_primary()
        while True:
            tok = self.peek()
            if self.check("."):
                self.next()
                name = self.expect_ident()
                if self.check("("):
                    expr = Call(expr, name, self.parse_args(), line=tok.line)
                else:
                    expr = FieldAccess(expr, name, line=tok.line)
            elif tok.kind == "OP" and tok.value in ("++", "--"):
                self.next()
                expr = IncDec(expr, tok.value, line=tok.line)
            else:
                return expr

    def parse_args(self) -> list:
        self.expect("(")
        args = []
        if not self.check(")"):
            while True:
                args.append(self.parse_expr())
                if not self.check(","):
                    break
                self.next()
        self.expect(")")
        return args

    def parse_primary(self) -> Node:
        tok = self.next()
        line = tok.line
        if tok.kind == "NUMBER":
            return IntLit(int(tok.value), line=line)
        if tok.kind == "STRING":
            return StrLit(bytes(tok.value[1:-1], "utf-8").decode("unicode_escape"), line=line)
        if tok.value in ("true", "false") and tok.kind == "KEYWORD":
            return BoolLit(tok.value == "true", line=line)
        if tok.value == "this" and tok.kind == "KEYWORD":
            return This(line=line)
        if tok.value == "new" and tok.kind == "KEYWORD":
            return New(self.expect_ident(), self.parse_args(), line=line)
        if tok.value == "(" and tok.kind == "OP":
            expr = self.parse_expr()
            self.expect(")")
            return expr
        if tok.kind == "IDENT":
            if self.check("("):
                return Call(None, tok.value, self.parse_args(), line=line)
            return Name(tok.value, line=line)
        raise CompileError(f"unexpected '{tok.value or 'end of file'}'", line)


# =====================================================================
# 4. CLASS FILE - the compiler's output
# =====================================================================

@dataclass
class Instruction:
    pc: int                 # byte offset inside the method, like real bytecode
    op: str                 # mnemonic, e.g. "iload_1"
    arg: object = None      # operand: number, symbolic reference, or jump target pc

    def to_dict(self) -> dict:
        d = {"pc": self.pc, "op": self.op}
        if self.arg is not None:
            d["arg"] = self.arg
        return d


@dataclass
class MethodInfo:
    name: str
    descriptor: str         # e.g. "(II)I" = takes two ints, returns int
    static: bool
    max_stack: int          # deepest the operand stack ever gets
    max_locals: int         # number of local variable slots needed
    code: list[Instruction]

    def to_dict(self) -> dict:
        return {
            "name": self.name, "descriptor": self.descriptor, "static": self.static,
            "max_stack": self.max_stack, "max_locals": self.max_locals,
            "code": [ins.to_dict() for ins in self.code],
        }


@dataclass
class ClassFile:
    name: str
    super_name: str
    fields: list[dict]
    methods: list[MethodInfo]

    def to_dict(self) -> dict:
        return {
            "magic": "0xCAFEBABE",
            "this_class": self.name,
            "super_class": self.super_name,
            "fields": self.fields,
            "methods": [m.to_dict() for m in self.methods],
        }

    def save(self, folder: str = ".") -> str:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{self.name}.class.json")
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        return path

    def disassemble(self) -> str:
        """Readable listing in the style of `javap -c`."""
        out = [f"class {self.name} extends {self.super_name} {{"]
        for f in self.fields:
            out.append(f"  {'static ' if f['static'] else ''}{f['descriptor']} {f['name']};")
        for m in self.methods:
            out.append("")
            out.append(f"  {'static ' if m.static else ''}{m.name}{m.descriptor}")
            out.append(f"    Code: stack={m.max_stack}, locals={m.max_locals}")
            for ins in m.code:
                arg = "" if ins.arg is None else f" {ins.arg}"
                out.append(f"      {ins.pc:>4}: {ins.op}{arg}")
        out.append("}")
        return "\n".join(out)


# =====================================================================
# 5. CODE GENERATOR - AST -> bytecode
# =====================================================================

PRIMITIVES = {"int": "I", "boolean": "Z", "void": "V"}
SPECIAL_REFS = {"String": "Ljava/lang/String;", "Object": "Ljava/lang/Object;"}
COMPARE = {"==": "eq", "!=": "ne", "<": "lt", "<=": "le", ">": "gt", ">=": "ge"}
NEGATE = {"==": "!=", "!=": "==", "<": ">=", ">=": "<", ">": "<=", "<=": ">"}
ARITH = {"+": "iadd", "-": "isub", "*": "imul", "/": "idiv", "%": "irem"}


def descriptor(typ: str) -> str:
    """Java type -> JVM type descriptor (int -> I, Point -> LPoint;, String[] -> [Ljava/lang/String;)."""
    dims = typ.count("[]")
    base = typ.replace("[]", "")
    d = PRIMITIVES.get(base) or SPECIAL_REFS.get(base) or f"L{base};"
    return "[" * dims + d


def is_ref(typ: str) -> bool:
    return typ not in ("int", "boolean")


@dataclass
class MethodSig:
    params: list[str]
    ret: str
    static: bool

    @property
    def descriptor(self) -> str:
        return "(" + "".join(descriptor(p) for p in self.params) + ")" + descriptor(self.ret)


@dataclass
class ClassInfo:
    name: str
    fields: dict      # name -> (type, static)
    methods: dict     # name -> MethodSig


class Label:
    """A jump target. Its position is filled in when it is placed."""
    def __init__(self):
        self.index: Optional[int] = None


class MethodGen:
    """Builds the bytecode for one method."""

    def __init__(self, cls: ClassInfo, static: bool):
        self.cls = cls
        self.static = static
        self.code: list[list] = []                      # [op, arg]
        self.scopes: list[dict] = [{}]                  # name -> (slot, type)
        self.next_slot = 0 if static else 1             # slot 0 holds `this` in instance methods
        self.depth = 0                                  # current operand stack depth
        self.max_depth = 0
        self.label_depth: dict[Label, int] = {}
        self.max_locals = self.next_slot                # most slots in use at any one time

    # ---- emitting instructions ----
    def emit(self, op: str, arg=None, delta: Optional[int] = None):
        self.code.append([op, arg])
        if delta is None:
            delta = self.stack_delta(op)
        self.depth += delta
        self.max_depth = max(self.max_depth, self.depth)
        if isinstance(arg, Label):
            self.label_depth.setdefault(arg, self.depth)

    @staticmethod
    def stack_delta(op: str) -> int:
        if op.startswith(("if_icmp", "if_acmp")) or op == "putfield":
            return -2
        if op.startswith(("iconst", "bipush", "sipush", "ldc", "iload", "aload", "getstatic", "new", "dup")):
            return 1
        if op in ARITH.values() or op.startswith(("istore", "astore", "if")) or op in (
                "pop", "ireturn", "areturn", "putstatic"):
            return -1
        return 0   # ineg, iinc, goto, getfield, return

    def place(self, label: Label):
        label.index = len(self.code)
        if label in self.label_depth:
            self.depth = self.label_depth[label]

    def push_int(self, v: int):
        if -1 <= v <= 5:
            self.emit("iconst_m1" if v == -1 else f"iconst_{v}")
        elif -128 <= v <= 127:
            self.emit("bipush", v)
        elif -32768 <= v <= 32767:
            self.emit("sipush", v)
        else:
            self.emit("ldc", v)

    def load(self, slot: int, typ: str):
        p = "a" if is_ref(typ) else "i"
        self.emit(f"{p}load_{slot}") if slot <= 3 else self.emit(f"{p}load", slot)

    def store(self, slot: int, typ: str):
        p = "a" if is_ref(typ) else "i"
        self.emit(f"{p}store_{slot}") if slot <= 3 else self.emit(f"{p}store", slot)

    # ---- local variables ----
    def declare(self, name: str, typ: str, line: int = 0) -> int:
        if name in self.scopes[-1]:
            raise CompileError(f"variable '{name}' is already defined", line)
        slot = self.next_slot
        self.scopes[-1][name] = (slot, typ)
        self.next_slot += 1
        self.max_locals = max(self.max_locals, self.next_slot)
        return slot

    def open_scope(self):
        self.scopes.append({})
        return self.next_slot

    def close_scope(self, saved_slot: int):
        # variables declared in the block are gone, so their slots can be reused (as real javac does)
        self.scopes.pop()
        self.next_slot = saved_slot

    def lookup(self, name: str):
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    # ---- turn labels into byte offsets ----
    def finish(self) -> tuple[list[Instruction], int, int]:
        def size(op: str) -> int:
            if op in ("bipush", "ldc", "iload", "istore", "aload", "astore"):
                return 2
            if op in ("sipush", "iinc", "goto", "new") or op.startswith(("if", "get", "put", "invoke")):
                return 3
            return 1

        pcs, pc = [], 0
        for op, _ in self.code:
            pcs.append(pc)
            pc += size(op)
        pcs.append(pc)

        out = []
        for (op, arg), at in zip(self.code, pcs):
            if isinstance(arg, Label):
                arg = pcs[arg.index]
            out.append(Instruction(at, op, arg))
        return out, self.max_depth, self.max_locals


class Compiler:
    def __init__(self, classes: list[ClassDecl]):
        self.decls = classes
        self.classes: dict[str, ClassInfo] = {}
        for c in classes:
            fields = {f.name: (f.type, f.static) for f in c.fields}
            methods = {m.name: MethodSig([p[0] for p in m.params], m.ret, m.static) for m in c.methods}
            methods.setdefault("<init>", MethodSig([], "void", False))   # default constructor
            self.classes[c.name] = ClassInfo(c.name, fields, methods)
        self.cls: ClassInfo = None
        self.m: MethodGen = None

    # ---- whole classes ----
    def compile(self) -> list[ClassFile]:
        return [self.compile_class(c) for c in self.decls]

    def compile_class(self, decl: ClassDecl) -> ClassFile:
        self.cls = self.classes[decl.name]
        fields = [{"name": f.name, "descriptor": descriptor(f.type), "static": f.static} for f in decl.fields]
        methods = []

        ctor = next((m for m in decl.methods if m.name == "<init>"), None)
        methods.append(self.compile_method(ctor or MethodDecl("<init>", "void", [], False, Block([])), decl))
        for m in decl.methods:
            if m.name != "<init>":
                methods.append(self.compile_method(m, decl))

        # static field initialisers run in the class initialiser <clinit>
        static_inits = [f for f in decl.fields if f.static and f.init is not None]
        if static_inits:
            self.m = MethodGen(self.cls, static=True)
            for f in static_inits:
                self.gen_expr(f.init)
                self.m.emit("putstatic", f"{decl.name}.{f.name}:{descriptor(f.type)}")
            self.m.emit("return")
            code, stack, locals_ = self.m.finish()
            methods.append(MethodInfo("<clinit>", "()V", True, stack, locals_, code))

        return ClassFile(decl.name, "java/lang/Object", fields, methods)

    def compile_method(self, decl: MethodDecl, cls_decl: ClassDecl) -> MethodInfo:
        self.m = MethodGen(self.cls, decl.static)
        for typ, name in decl.params:
            self.m.declare(name, typ, decl.line)

        if decl.name == "<init>":
            # every constructor first calls the superclass constructor ...
            self.m.emit("aload_0")
            self.m.emit("invokespecial", "java/lang/Object.<init>:()V", delta=-1)
            # ... then runs the instance field initialisers
            for f in cls_decl.fields:
                if not f.static and f.init is not None:
                    self.m.emit("aload_0")
                    self.gen_expr(f.init)
                    self.m.emit("putfield", f"{cls_decl.name}.{f.name}:{descriptor(f.type)}")

        for stmt in decl.body.stmts:
            self.gen_stmt(stmt, decl)

        if decl.ret == "void" and (not self.m.code or self.m.code[-1][0] != "return"):
            self.m.emit("return")

        code, stack, locals_ = self.m.finish()
        sig = self.cls.methods[decl.name]
        return MethodInfo(decl.name, sig.descriptor, decl.static, stack, locals_, code)

    # ---- statements ----
    def gen_stmt(self, s: Node, method: MethodDecl):
        m = self.m
        if isinstance(s, Block):
            saved = m.open_scope()
            for st in s.stmts:
                self.gen_stmt(st, method)
            m.close_scope(saved)

        elif isinstance(s, VarDecl):
            if s.init is not None:
                self.gen_expr(s.init)
            slot = m.declare(s.name, s.type, s.line)
            if s.init is not None:
                m.store(slot, s.type)

        elif isinstance(s, ExprStmt):
            e = s.expr
            if isinstance(e, (Assign, IncDec)):
                self.gen_assign(e)
            elif isinstance(e, Call):
                if self.gen_expr(e) != "void":
                    m.emit("pop")            # discard an unused return value
            else:
                raise CompileError("not a statement", s.line)

        elif isinstance(s, If):
            else_label, end_label = Label(), Label()
            self.gen_cond(s.cond, else_label, jump_if=False)
            self.gen_stmt(s.then, method)
            if s.else_:
                m.emit("goto", end_label)
                m.place(else_label)
                self.gen_stmt(s.else_, method)
                m.place(end_label)
            else:
                m.place(else_label)

        elif isinstance(s, While):
            start, end = Label(), Label()
            m.place(start)
            self.gen_cond(s.cond, end, jump_if=False)
            self.gen_stmt(s.body, method)
            m.emit("goto", start)
            m.place(end)

        elif isinstance(s, For):
            saved = m.open_scope()
            if s.init:
                self.gen_stmt(s.init, method)
            start, end = Label(), Label()
            m.place(start)
            if s.cond:
                self.gen_cond(s.cond, end, jump_if=False)
            self.gen_stmt(s.body, method)
            if s.update:
                self.gen_stmt(s.update, method)
            m.emit("goto", start)
            m.place(end)
            m.close_scope(saved)

        elif isinstance(s, Return):
            if s.expr is None:
                m.emit("return")
            else:
                typ = self.gen_expr(s.expr)
                m.emit("areturn" if is_ref(typ) else "ireturn")
        else:
            raise CompileError(f"unsupported statement {type(s).__name__}", s.line)

    # ---- assignment, ++ and -- ----
    def gen_assign(self, e: Node):
        m = self.m
        if isinstance(e, IncDec):
            op, value = ("+=" if e.op == "++" else "-="), IntLit(1, line=e.line)
        else:
            op, value = e.op, e.value
        target = e.target

        # local variable
        if isinstance(target, Name) and m.lookup(target.name):
            slot, typ = m.lookup(target.name)
            if op in ("+=", "-=") and typ == "int" and isinstance(value, IntLit) and -128 <= value.value <= 127:
                m.emit("iinc", f"{slot} {value.value if op == '+=' else -value.value}")
                return
            if op != "=":
                m.load(slot, typ)
            self.gen_expr(value)
            if op != "=":
                m.emit(ARITH[op[0]])
            m.store(slot, typ)
            return

        # field
        owner, ftype, static = self.resolve_field_target(target)
        ref = f"{owner}.{target.name}:{descriptor(ftype)}"
        if static:
            if op != "=":
                m.emit("getstatic", ref)
            self.gen_expr(value)
            if op != "=":
                m.emit(ARITH[op[0]])
            m.emit("putstatic", ref)
        else:
            if op != "=":
                m.emit("dup")
                m.emit("getfield", ref)
            self.gen_expr(value)
            if op != "=":
                m.emit(ARITH[op[0]])
            m.emit("putfield", ref)

    def resolve_field_target(self, target: Node):
        """For a field being assigned: push the object (if instance field) and return (owner, type, static)."""
        if isinstance(target, Name):
            info = self.cls.fields.get(target.name)
            if not info:
                raise CompileError(f"cannot find variable '{target.name}'", target.line)
            typ, static = info
            if not static:
                self.need_this(target.line)
                self.m.emit("aload_0")
            return self.cls.name, typ, static
        # obj.field
        obj = target.obj
        if isinstance(obj, Name) and not self.m.lookup(obj.name) and obj.name in self.classes:
            typ, static = self.field_info(obj.name, target.name, target.line)
            return obj.name, typ, True
        owner = self.gen_expr(obj)
        typ, static = self.field_info(owner, target.name, target.line)
        return owner, typ, False

    def field_info(self, owner: str, name: str, line: int):
        cls = self.classes.get(owner)
        if not cls or name not in cls.fields:
            raise CompileError(f"cannot find field '{name}' in '{owner}'", line)
        return cls.fields[name]

    def need_this(self, line: int):
        if self.m.static:
            raise CompileError("cannot use instance members from a static method", line)

    # ---- expressions: emit code that leaves the value on the stack, return its type ----
    def gen_expr(self, e: Node) -> str:
        m = self.m
        if isinstance(e, IntLit):
            m.push_int(e.value)
            return "int"
        if isinstance(e, BoolLit):
            m.push_int(1 if e.value else 0)
            return "boolean"
        if isinstance(e, StrLit):
            m.emit("ldc", json.dumps(e.value))
            return "String"
        if isinstance(e, This):
            self.need_this(e.line)
            m.emit("aload_0")
            return self.cls.name

        if isinstance(e, Name):
            local = m.lookup(e.name)
            if local:
                m.load(*local)
                return local[1]
            if e.name in self.cls.fields:
                typ, static = self.cls.fields[e.name]
                ref = f"{self.cls.name}.{e.name}:{descriptor(typ)}"
                if static:
                    m.emit("getstatic", ref)
                else:
                    self.need_this(e.line)
                    m.emit("aload_0")
                    m.emit("getfield", ref)
                return typ
            raise CompileError(f"cannot find variable '{e.name}'", e.line)

        if isinstance(e, FieldAccess):
            obj = e.obj
            if isinstance(obj, Name) and not m.lookup(obj.name) and obj.name in self.classes:
                typ, _ = self.field_info(obj.name, e.name, e.line)
                m.emit("getstatic", f"{obj.name}.{e.name}:{descriptor(typ)}")
                return typ
            owner = self.gen_expr(obj)
            typ, _ = self.field_info(owner, e.name, e.line)
            m.emit("getfield", f"{owner}.{e.name}:{descriptor(typ)}")
            return typ

        if isinstance(e, Call):
            return self.gen_call(e)

        if isinstance(e, New):
            cls = self.classes.get(e.cls)
            if not cls:
                raise CompileError(f"cannot find class '{e.cls}'", e.line)
            sig = cls.methods["<init>"]
            self.check_args(sig, e.args, f"constructor {e.cls}", e.line)
            m.emit("new", e.cls)           # allocate the object on the heap
            m.emit("dup")                   # one copy for the constructor, one to keep
            for a in e.args:
                self.gen_expr(a)
            m.emit("invokespecial", f"{e.cls}.<init>:{sig.descriptor}", delta=-(len(e.args) + 1))
            return e.cls

        if isinstance(e, Unary) and e.op == "-":
            if isinstance(e.expr, IntLit):
                m.push_int(-e.expr.value)
                return "int"
            self.expect_int(self.gen_expr(e.expr), e.line)
            m.emit("ineg")
            return "int"

        if isinstance(e, Binary) and e.op in ARITH:
            self.expect_int(self.gen_expr(e.left), e.line)
            self.expect_int(self.gen_expr(e.right), e.line)
            m.emit(ARITH[e.op])
            return "int"

        if isinstance(e, (Binary, Unary)):          # comparisons, && || !  -> 0 or 1
            false_label, end = Label(), Label()
            self.gen_cond(e, false_label, jump_if=False)
            m.emit("iconst_1")
            m.emit("goto", end)
            m.place(false_label)
            m.emit("iconst_0")
            m.place(end)
            return "boolean"

        if isinstance(e, (Assign, IncDec)):
            raise CompileError("assignments and ++/-- can only be used as statements in this subset", e.line)
        raise CompileError(f"unsupported expression {type(e).__name__}", e.line)

    def expect_int(self, typ: str, line: int):
        if typ != "int":
            raise CompileError(f"expected int but got {typ}", line)

    # ---- conditions: jump to `label` when the condition equals `jump_if` ----
    def gen_cond(self, e: Node, label: Label, jump_if: bool):
        m = self.m
        if isinstance(e, Binary) and e.op in COMPARE:
            lt = self.gen_expr(e.left)
            self.gen_expr(e.right)
            op = e.op if jump_if else NEGATE[e.op]
            prefix = "if_acmp" if is_ref(lt) else "if_icmp"
            m.emit(prefix + COMPARE[op], label)
        elif isinstance(e, Binary) and e.op == "&&":
            if jump_if:
                skip = Label()
                self.gen_cond(e.left, skip, False)
                self.gen_cond(e.right, label, True)
                m.place(skip)
            else:
                self.gen_cond(e.left, label, False)
                self.gen_cond(e.right, label, False)
        elif isinstance(e, Binary) and e.op == "||":
            if jump_if:
                self.gen_cond(e.left, label, True)
                self.gen_cond(e.right, label, True)
            else:
                skip = Label()
                self.gen_cond(e.left, skip, True)
                self.gen_cond(e.right, label, False)
                m.place(skip)
        elif isinstance(e, Unary) and e.op == "!":
            self.gen_cond(e.expr, label, not jump_if)
        elif isinstance(e, BoolLit):
            if e.value == jump_if:
                m.emit("goto", label)
        else:
            self.gen_expr(e)
            m.emit("ifne" if jump_if else "ifeq", label)

    # ---- method calls ----
    def gen_call(self, e: Call) -> str:
        m = self.m

        # System.out.println(x) / System.out.print(x)
        if (isinstance(e.obj, FieldAccess) and isinstance(e.obj.obj, Name)
                and e.obj.obj.name == "System" and e.obj.name == "out" and e.name in ("println", "print")):
            m.emit("getstatic", "java/lang/System.out:Ljava/io/PrintStream;")
            if not e.args:
                arg_desc = ""
            elif len(e.args) == 1:
                typ = self.gen_expr(e.args[0])
                arg_desc = descriptor(typ) if typ in ("int", "boolean", "String") else "Ljava/lang/Object;"
            else:
                raise CompileError(f"{e.name} takes at most one argument", e.line)
            m.emit("invokevirtual", f"java/io/PrintStream.{e.name}:({arg_desc})V", delta=-(1 + len(e.args)))
            return "void"

        # work out which class owns the method and whether it is static
        if e.obj is None:
            owner = self.cls.name
            sig = self.method_sig(owner, e.name, e.line)
            if not sig.static:
                self.need_this(e.line)
                m.emit("aload_0")
        elif isinstance(e.obj, Name) and not m.lookup(e.obj.name) and e.obj.name in self.classes:
            owner = e.obj.name                              # ClassName.staticMethod()
            sig = self.method_sig(owner, e.name, e.line)
            if not sig.static:
                raise CompileError(f"'{e.name}' is not static", e.line)
        else:
            owner = self.gen_expr(e.obj)                    # obj.method()
            sig = self.method_sig(owner, e.name, e.line)

        self.check_args(sig, e.args, f"method {e.name}", e.line)
        for a in e.args:
            self.gen_expr(a)

        pops = len(e.args) + (0 if sig.static else 1)
        pushes = 0 if sig.ret == "void" else 1
        op = "invokestatic" if sig.static else "invokevirtual"
        m.emit(op, f"{owner}.{e.name}:{sig.descriptor}", delta=pushes - pops)
        return sig.ret

    def method_sig(self, owner: str, name: str, line: int) -> MethodSig:
        cls = self.classes.get(owner)
        if not cls or name not in cls.methods:
            raise CompileError(f"cannot find method '{name}' in '{owner}'", line)
        return cls.methods[name]

    def check_args(self, sig: MethodSig, args: list, what: str, line: int):
        if len(sig.params) != len(args):
            raise CompileError(f"{what} expects {len(sig.params)} argument(s) but got {len(args)}", line)


# =====================================================================
# 6. THE JAVAC CLASS - ties the stages together
# =====================================================================

class Javac:
    """
    A miniature javac.

        javac = Javac()
        class_files = javac.compile_file("Main.java")
        for cf in class_files:
            print(cf.disassemble())
            cf.save("out")
    """

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def log(self, msg: str):
        if self.verbose:
            print(f"[javac] {msg}")

    def compile_source(self, source: str) -> list[ClassFile]:
        tokens = tokenize(source)
        self.log(f"lexer produced {len(tokens) - 1} tokens")

        classes = Parser(tokens).parse_program()
        self.log(f"parser built an AST with {len(classes)} class(es): {', '.join(c.name for c in classes)}")

        class_files = Compiler(classes).compile()
        for cf in class_files:
            n = sum(len(mt.code) for mt in cf.methods)
            self.log(f"code generator wrote {cf.name}: {len(cf.methods)} method(s), {n} instruction(s)")
        return class_files

    def compile_file(self, path: str, out_dir: Optional[str] = None) -> list[ClassFile]:
        with open(path) as f:
            class_files = self.compile_source(f.read())
        folder = out_dir or os.path.dirname(os.path.abspath(path))
        for cf in class_files:
            self.log(f"wrote {cf.save(folder)}")
        return class_files


def main(argv: list[str]):
    if not argv:
        print(__doc__)
        return 1
    out_dir = None
    if "-o" in argv:
        i = argv.index("-o")
        out_dir = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    try:
        for cf in Javac(verbose=True).compile_file(argv[0], out_dir):
            print()
            print(cf.disassemble())
    except CompileError as err:
        print(f"{argv[0]}: error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))