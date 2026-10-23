"""
jvm_sim.py - watch a Java program run on a (simulated) computer, step by step.

    python jvm_sim.py Main.java              # full trace
    python jvm_sim.py Main.java --step       # press Enter to advance each step
    python jvm_sim.py Main.java --delay 0.3  # auto-play with a pause between steps
    python jvm_sim.py Main.java --brief      # hide the per-bytecode detail
    python jvm_sim.py Main.java --out run.txt  # save the trace to run.txt (default: trace.txt)
    python jvm_sim.py Main.java --quiet      # write only to the file, not the screen

What happens, in order:
    Stage 1  javac      compiles Main.java into bytecode, saved to the Disk
    Stage 2  OS         starts the JVM as a process and gives it memory and a CPU
    Stage 3  JVM        boots: creates the Heap, the thread Stack, the ClassLoader
    Stage 4  ClassLoader reads .class files from the Disk (via OS system calls),
                        verifies them, links them and initializes them
    Stage 5  Execution  the Interpreter runs bytecode on the Stack, objects live on
                        the Heap, hot methods are JIT-compiled to machine code
                        and run directly on the CPU
    Stage 6  Output     println becomes an OS system call that sends text to the
                        Console device; then the process exits

Classes in this file:
    Tracer, Disk, Console, Devices, CPU, OS, Process,
    Heap, Frame, Stack, LoadedClass, ClassLoader, JIT, JVM
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from javac import CompileError, Compiler, Parser, tokenize


# =====================================================================
# Tracer - prints every step so you can follow the whole process
# =====================================================================

class Tracer:
    def __init__(self, step_mode: bool = False, delay: float = 0.0, brief: bool = False):
        self.n = 0
        self.step_mode = step_mode
        self.delay = delay
        self.brief = brief

    def stage(self, number: int, title: str, blurb: str = ""):
        print()
        print("=" * 78)
        print(f" STAGE {number}: {title}")
        if blurb:
            print(f" {blurb}")
        print("=" * 78)

    def step(self, who: str, msg: str):
        self.n += 1
        print(f"[{self.n:03}] {who:<12}| {msg}")
        self._pause()

    def detail(self, msg: str, always: bool = False):
        if always or not self.brief:
            print(f"      {'':<12}|   {msg}")

    def box(self, title: str, lines: list[str]):
        width = max([len(title) + 4] + [len(l) + 4 for l in lines])
        print(f"      {'':<12}|   +-- {title} " + "-" * (width - len(title) - 3) + "+")
        for l in lines:
            print(f"      {'':<12}|   | {l:<{width}}|")
        print(f"      {'':<12}|   +" + "-" * (width + 1) + "+")

    def _pause(self):
        if self.step_mode:
            input("")
        elif self.delay:
            time.sleep(self.delay)


# =====================================================================
# Values that live in the JVM
# =====================================================================

def i32(x: int) -> int:
    """Java ints are 32-bit and wrap around on overflow."""
    x &= 0xFFFFFFFF
    return x - 0x1_0000_0000 if x & 0x8000_0000 else x


class Ref:
    """A reference (pointer) to an object on the heap."""
    def __init__(self, addr: int, cls: str):
        self.addr, self.cls = addr, cls

    def __eq__(self, other):
        return isinstance(other, Ref) and other.addr == self.addr

    def __repr__(self):
        return f"{self.cls}@{self.addr:#x}"


class SystemOut:
    def __repr__(self):
        return "System.out"


SYSTEM_OUT = SystemOut()


def show(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, str):
        return json.dumps(v)
    return repr(v)


def show_list(values) -> str:
    return "[" + ", ".join(show(v) for v in values) + "]"


# =====================================================================
# DEVICES - hardware the OS talks to
# =====================================================================

class Disk:
    """Persistent storage. Class files are saved here and read back later."""

    def __init__(self, folder: str, t: Tracer):
        self.folder, self.t = folder, t
        os.makedirs(folder, exist_ok=True)

    def write(self, name: str, data: bytes):
        with open(os.path.join(self.folder, name), "wb") as f:
            f.write(data)
        self.t.step("Disk", f"wrote {name} ({len(data)} bytes) to storage")

    def read(self, name: str) -> bytes:
        path = os.path.join(self.folder, name)
        if not os.path.exists(path):
            raise FileNotFoundError(name)
        with open(path, "rb") as f:
            data = f.read()
        self.t.step("Disk", f"read {name} ({len(data)} bytes) from storage into RAM")
        return data


class Console:
    """The screen. Text written to standard output appears here."""

    def __init__(self, t: Tracer):
        self.t = t
        self.text = ""

    def write(self, text: str):
        self.text += text
        self.t.step("Console", f"screen shows: {text.rstrip()}")


class Devices:
    def __init__(self, folder: str, t: Tracer):
        self.disk = Disk(folder, t)
        self.console = Console(t)


# =====================================================================
# CPU - executes machine code
# =====================================================================

ARG_REGS = ["edi", "esi", "edx", "ecx"]                 # x86-64 calling convention: first args
SCRATCH_REGS = ["eax", "r8d", "r9d", "r10d", "r11d"]   # working registers, eax holds the result


class CPU:
    def __init__(self, t: Tracer):
        self.t = t
        self.registers = {r: 0 for r in ARG_REGS + SCRATCH_REGS}
        self.bytecodes = 0           # bytecodes run through the interpreter loop
        self.native = 0              # machine instructions run directly

    def run_native(self, name: str, code: list[tuple], args: list[int]) -> int:
        for reg, value in zip(ARG_REGS, args):
            self.registers[reg] = value
        loaded = ", ".join(f"{r}={v}" for r, v in zip(ARG_REGS, args))
        self.t.step("CPU", f"jumps straight into machine code for {name} ({loaded})")
        for ins in code:
            self.native += 1
            op = ins[0]
            if op == "ret":
                result = self.registers["eax"]
                self.t.detail(f"{'ret':<18}; return value is in eax = {result}", always=True)
                return result
            dst = ins[1]
            if op == "mov":
                self.registers[dst] = self._value(ins[2])
            elif op == "add":
                self.registers[dst] = i32(self.registers[dst] + self._value(ins[2]))
            elif op == "sub":
                self.registers[dst] = i32(self.registers[dst] - self._value(ins[2]))
            elif op == "imul":
                self.registers[dst] = i32(self.registers[dst] * self._value(ins[2]))
            elif op == "neg":
                self.registers[dst] = i32(-self.registers[dst])
            self.t.detail(f"{asm_text(ins):<18}; {dst} = {self.registers[dst]}", always=True)
        raise RuntimeError("machine code ended without ret")

    def _value(self, src):
        return src if isinstance(src, int) else self.registers[src]


def asm_text(ins: tuple) -> str:
    return ins[0] if len(ins) == 1 else f"{ins[0]} " + ", ".join(str(x) for x in ins[1:])


# =====================================================================
# OS - manages processes, memory and devices
# =====================================================================

class Process:
    def __init__(self, pid: int, command: str):
        self.pid = pid
        self.command = command
        self.memory: dict[str, tuple[int, int]] = {}
        self.state = "new"


class OS:
    def __init__(self, devices: Devices, cpu: CPU, t: Tracer):
        self.devices, self.cpu, self.t = devices, cpu, t
        self.next_pid = 4242

    def spawn(self, command: str) -> Process:
        t = self.t
        t.step("OS", f"user typed `{command}` in the terminal")
        proc = Process(self.next_pid, command)
        self.next_pid += 1
        t.step("OS", f"found the `java` program on disk and created process PID {proc.pid}")
        proc.memory = {
            "code (JVM program)": (0x0040_0000, 16 * 1024 * 1024),
            "heap (objects)":     (0x1000,      64 * 1024 * 1024),
            "stack (main thread)": (0x7FF0_0000, 1 * 1024 * 1024),
        }
        t.step("OS", "gave the process its own virtual memory:")
        t.box(f"PID {proc.pid} memory map", [
            f"{name:<20} starts {start:#011x}  size {size // (1024 * 1024)} MB"
            for name, (start, size) in proc.memory.items()
        ])
        proc.state = "running"
        t.step("OS", f"scheduler put PID {proc.pid} on CPU core 0 -> the JVM starts running")
        return proc

    # ---- system calls: the only way a program can use devices ----
    def sys_read(self, proc: Process, filename: str) -> bytes:
        self.t.step("OS", f"system call read(\"{filename}\") from PID {proc.pid} -> asking the Disk")
        return self.devices.disk.read(filename)

    def sys_write(self, proc: Process, fd: int, text: str):
        self.t.step("OS", f"system call write(fd={fd} stdout, {json.dumps(text)}) from PID {proc.pid}"
                          " -> sending to the Console")
        self.devices.console.write(text)

    def exit(self, proc: Process, code: int):
        proc.state = "terminated"
        self.t.step("OS", f"PID {proc.pid} exited with status {code}; its memory is freed "
                          "and the CPU is given to other processes")


# =====================================================================
# HEAP - where objects live
# =====================================================================

class Heap:
    def __init__(self, t: Tracer, base: int = 0x1000):
        self.t = t
        self.objects: dict[int, dict] = {}
        self.next_addr = base

    def allocate(self, cls: str, field_names: list[str]) -> Ref:
        addr = self.next_addr
        self.next_addr += 8 + 4 * len(field_names)        # header + one int per field
        self.objects[addr] = {"class": cls, "fields": {f: 0 for f in field_names}}
        ref = Ref(addr, cls)
        fields = ", ".join(f"{f}=0" for f in field_names)
        self.t.step("Heap", f"allocated a {cls} object at address {addr:#x} ({fields}, defaults)")
        return ref

    def _obj(self, ref) -> dict:
        if ref is None:
            raise RuntimeError("NullPointerException")
        return self.objects[ref.addr]

    def get_field(self, ref: Ref, name: str):
        value = self._obj(ref)["fields"][name]
        self.t.detail(f"Heap: read {ref}.{name} -> {value}")
        return value

    def put_field(self, ref: Ref, name: str, value):
        self._obj(ref)["fields"][name] = value
        self.t.step("Heap", f"{ref}.{name} = {show(value)}")

    def snapshot(self) -> list[str]:
        if not self.objects:
            return ["(empty)"]
        return [f"{addr:#06x}: {o['class']} {{"
                + ", ".join(f"{k}={show(v)}" for k, v in o["fields"].items()) + "}"
                for addr, o in self.objects.items()]


# =====================================================================
# STACK - one frame per method call that is currently running
# =====================================================================

class Frame:
    def __init__(self, name: str, max_locals: int, args: list, native: bool = False):
        self.name = name
        self.locals = list(args) + [None] * (max_locals - len(args))
        self.operands: list = []           # the operand stack used by bytecode
        self.native = native

    def label(self) -> str:
        if self.native:
            return f"{self.name} [machine code, uses CPU registers]"
        return f"{self.name}  locals={show_list(self.locals)}  operands={show_list(self.operands)}"


class Stack:
    def __init__(self, t: Tracer, max_frames: int = 1000):
        self.t = t
        self.frames: list[Frame] = []
        self.max_frames = max_frames
        self.deepest = 0

    def push(self, frame: Frame):
        if len(self.frames) >= self.max_frames:
            raise RuntimeError("StackOverflowError")
        self.frames.append(frame)
        self.deepest = max(self.deepest, len(self.frames))
        self.t.step("Stack", f"push frame for {frame.name} (depth {len(self.frames)})")
        self.t.box("thread stack (top first)", self.snapshot())

    def pop(self) -> Frame:
        frame = self.frames.pop()
        back_to = self.frames[-1].name if self.frames else "the JVM launcher"
        self.t.step("Stack", f"pop frame for {frame.name}, control returns to {back_to}")
        return frame

    def snapshot(self) -> list[str]:
        return [f.label() for f in reversed(self.frames)] or ["(empty)"]


# =====================================================================
# CLASS LOADER - brings classes from disk into the JVM
# =====================================================================

class LoadedClass:
    def __init__(self, cf: dict):
        self.name = cf["this_class"]
        self.methods = {m["name"]: m for m in cf["methods"]}
        self.instance_fields = [f["name"] for f in cf["fields"] if not f["static"]]
        self.statics = {f["name"]: 0 for f in cf["fields"] if f["static"]}


class ClassLoader:
    def __init__(self, jvm: "JVM"):
        self.jvm = jvm
        self.t = jvm.t
        self.classes: dict[str, LoadedClass] = {}

    def load(self, name: str) -> LoadedClass:
        if name in self.classes:
            return self.classes[name]
        t, jvm = self.t, self.jvm
        t.step("ClassLoader", f"class {name} is needed for the first time -> load it")

        # 1. Loading: get the bytes from disk through the OS
        data = jvm.os.sys_read(jvm.proc, f"{name}.class.json")
        cf = json.loads(data)

        # 2. Verification: make sure the bytecode is safe to run
        if cf.get("magic") != "0xCAFEBABE":
            raise RuntimeError(f"ClassFormatError: {name} is not a class file")
        for m in cf["methods"]:
            pcs = {ins["pc"] for ins in m["code"]}
            for ins in m["code"]:
                if ins["op"].startswith(("if", "goto")) and ins["arg"] not in pcs:
                    raise RuntimeError(f"VerifyError: bad jump in {name}.{m['name']}")
        t.step("ClassLoader", f"verify {name}: magic number 0xCAFEBABE OK, all jumps land on real instructions")

        # 3. Linking: build the in-memory structures (method area)
        cls = LoadedClass(cf)
        self.classes[name] = cls
        methods = ", ".join(cls.methods)
        t.step("ClassLoader", f"link {name}: method table [{methods}]"
               + (f", static fields {cls.statics}" if cls.statics else ""))

        # 4. Initialization: run the static initializer if there is one
        if "<clinit>" in cls.methods:
            t.step("ClassLoader", f"initialize {name}: run its static initializer <clinit>")
            jvm.invoke(name, "<clinit>", [])
        else:
            t.step("ClassLoader", f"initialize {name}: nothing to run, class is ready")
        return cls


# =====================================================================
# JIT - turns hot bytecode into machine code
# =====================================================================

class JIT:
    SUPPORTED = ("iload", "iconst", "bipush", "sipush", "iadd", "isub", "imul", "ineg", "ireturn")

    def __init__(self, t: Tracer, threshold: int):
        self.t = t
        self.threshold = threshold
        self.calls: dict[str, int] = {}
        self.compiled: dict[str, list[tuple]] = {}
        self.rejected: set[str] = set()

    def record_call(self, key: str) -> int:
        self.calls[key] = self.calls.get(key, 0) + 1
        return self.calls[key]

    def maybe_compile(self, key: str, method: dict):
        n = self.calls.get(key, 0)
        if n < self.threshold or key in self.compiled or key in self.rejected:
            return
        self.t.step("JIT", f"{key} has been called {n} times -> it is HOT, compile it to machine code")
        code = self.translate(method)
        if code is None:
            self.rejected.add(key)
            self.t.detail("this tiny JIT only handles simple int maths; it stays interpreted", always=True)
            return
        self.compiled[key] = code
        lines = []
        bytecode = [f"{i['op']}{' ' + str(i['arg']) if 'arg' in i else ''}" for i in method["code"]]
        for k in range(max(len(bytecode), len(code))):
            left = bytecode[k] if k < len(bytecode) else ""
            right = asm_text(code[k]) if k < len(code) else ""
            lines.append(f"{left:<14} =>  {right}")
        self.t.box(f"{key}: bytecode => x86-64 machine code", lines)
        self.t.step("JIT", f"machine code for {key} installed; next calls skip the interpreter")

    def translate(self, method: dict):
        """Map the operand stack onto CPU registers, one bytecode at a time."""
        if not method["static"]:
            return None
        asm, free, vstack = [], list(SCRATCH_REGS), []
        for ins in method["code"]:
            op, arg = ins["op"], ins.get("arg")
            if not op.startswith(self.SUPPORTED):
                return None
            if op.startswith("iload"):
                slot = int(op.split("_")[1]) if "_" in op else arg
                reg = free.pop(0)
                asm.append(("mov", reg, ARG_REGS[slot]))
                vstack.append(reg)
            elif op.startswith(("iconst", "bipush", "sipush")):
                value = -1 if op == "iconst_m1" else (int(op.split("_")[1]) if "_" in op else arg)
                reg = free.pop(0)
                asm.append(("mov", reg, value))
                vstack.append(reg)
            elif op in ("iadd", "isub", "imul"):
                b, a = vstack.pop(), vstack.pop()
                asm.append(({"iadd": "add", "isub": "sub", "imul": "imul"}[op], a, b))
                free.insert(0, b)
                vstack.append(a)
            elif op == "ineg":
                asm.append(("neg", vstack[-1]))
            elif op == "ireturn":
                reg = vstack.pop()
                if reg != "eax":
                    asm.append(("mov", "eax", reg))
                asm.append(("ret",))
        return asm


# =====================================================================
# JVM - ties it all together and contains the bytecode interpreter
# =====================================================================

def count_args(desc: str) -> int:
    params, n, i = desc[desc.index("(") + 1: desc.index(")")], 0, 0
    while i < len(params):
        while params[i] == "[":
            i += 1
        i = params.index(";", i) + 1 if params[i] == "L" else i + 1
        n += 1
    return n


def split_ref(ref: str):
    """'Point.sum:()I' -> ('Point', 'sum', '()I')"""
    owner_name, desc = ref.split(":", 1)
    owner, name = owner_name.rsplit(".", 1)
    return owner, name, desc


COMPARE = {"eq": lambda a, b: a == b, "ne": lambda a, b: a != b, "lt": lambda a, b: a < b,
           "le": lambda a, b: a <= b, "gt": lambda a, b: a > b, "ge": lambda a, b: a >= b}


class JVM:
    def __init__(self, os_: OS, proc: Process, t: Tracer, jit_threshold: int):
        self.os, self.proc, self.t = os_, proc, t
        self.cpu = os_.cpu
        t.step("JVM", "booting: setting up the runtime data areas")
        self.heap = Heap(t, proc.memory["heap (objects)"][0])
        t.detail(f"Heap created at {self.heap.next_addr:#x} for objects")
        self.stack = Stack(t)
        t.detail("Stack created for the main thread (one frame per running method)")
        self.loader = ClassLoader(self)
        t.detail("ClassLoader ready to fetch .class files")
        self.jit = JIT(t, jit_threshold)
        t.detail(f"Interpreter ready; JIT will compile a method after {jit_threshold} calls")

    def run_main(self, class_name: str):
        self.t.step("JVM", f"looking for {class_name}.main(String[] args)")
        self.loader.load(class_name)
        self.t.step("JVM", f"calling {class_name}.main")
        self.invoke(class_name, "main", ["String[0]"])

    # ---- calling a method: interpreted or native ----
    def invoke(self, cls_name: str, name: str, args: list):
        cls = self.loader.load(cls_name)
        method = cls.methods[name]
        key = f"{cls_name}.{name}"
        n = self.jit.record_call(key)
        if key in self.jit.compiled:
            self.t.step("JVM", f"call #{n} of {key}: compiled version exists -> run on CPU directly")
            frame = Frame(key, 0, [], native=True)
            self.stack.push(frame)
            result = self.cpu.run_native(key, self.jit.compiled[key], args)
            self.stack.pop()
            return result
        if name not in ("<clinit>", "main"):
            self.t.step("JVM", f"call #{n} of {key}: not compiled -> the interpreter runs it")
        result = self.interpret(cls, method, args)
        self.jit.maybe_compile(key, method)
        return result

    # ---- the interpreter: a loop that runs one bytecode at a time ----
    def interpret(self, cls: LoadedClass, method: dict, args: list):
        t = self.t
        frame = Frame(f"{cls.name}.{method['name']}", method["max_locals"], args)
        self.stack.push(frame)
        code = method["code"]
        index_of = {ins["pc"]: i for i, ins in enumerate(code)}
        ops = frame.operands
        i = 0

        while True:
            ins = code[i]
            op, arg = ins["op"], ins.get("arg")
            self.cpu.bytecodes += 1
            jump = None
            note = ""
            text = f"{frame.name} pc={ins['pc']:<3} {op}{' ' + str(arg) if arg is not None else ''}"
            if not t.brief:
                t.step("Interpreter", text)

            # --- constants ---
            if op.startswith("iconst"):
                v = -1 if op == "iconst_m1" else int(op.split("_")[1])
                ops.append(v); note = f"push constant {v}"
            elif op in ("bipush", "sipush"):
                ops.append(arg); note = f"push constant {arg}"
            elif op == "ldc":
                v = json.loads(arg) if isinstance(arg, str) else arg
                ops.append(v); note = f"push constant {show(v)}"

            # --- local variables ---
            elif op.startswith(("iload", "aload")):
                slot = int(op.split("_")[1]) if "_" in op else arg
                ops.append(frame.locals[slot]); note = f"push local[{slot}] = {show(frame.locals[slot])}"
            elif op.startswith(("istore", "astore")):
                slot = int(op.split("_")[1]) if "_" in op else arg
                frame.locals[slot] = ops.pop(); note = f"pop {show(frame.locals[slot])} into local[{slot}]"
            elif op == "iinc":
                slot, delta = (int(x) for x in str(arg).split())
                frame.locals[slot] = i32(frame.locals[slot] + delta)
                note = f"local[{slot}] += {delta} -> {frame.locals[slot]}"

            # --- arithmetic ---
            elif op in ("iadd", "isub", "imul", "idiv", "irem"):
                b, a = ops.pop(), ops.pop()
                if op in ("idiv", "irem") and b == 0:
                    raise RuntimeError("ArithmeticException: / by zero")
                q = int(a / b) if op in ("idiv", "irem") else 0
                r = {"iadd": a + b, "isub": a - b, "imul": a * b, "idiv": q, "irem": a - b * q}[op]
                ops.append(i32(r))
                sym = {"iadd": "+", "isub": "-", "imul": "*", "idiv": "/", "irem": "%"}[op]
                note = f"pop {a} and {b}, CPU computes {a} {sym} {b} = {i32(r)}, push it"
            elif op == "ineg":
                ops.append(i32(-ops.pop())); note = f"negate -> {ops[-1]}"

            # --- jumps ---
            elif op == "goto":
                jump = arg; note = f"jump to pc {arg}"
            elif op.startswith(("if_icmp", "if_acmp")):
                b, a = ops.pop(), ops.pop()
                taken = COMPARE[op[-2:]](a, b)
                note = f"compare {show(a)} {op[-2:]} {show(b)}: {'true -> jump to ' + str(arg) if taken else 'false -> continue'}"
                if taken:
                    jump = arg
            elif op in ("ifeq", "ifne"):
                v = ops.pop()
                taken = (v == 0) if op == "ifeq" else (v != 0)
                note = f"{v} {'== 0' if op == 'ifeq' else '!= 0'}: {'jump' if taken else 'continue'}"
                if taken:
                    jump = arg

            # --- operand stack housekeeping ---
            elif op == "dup":
                ops.append(ops[-1]); note = f"duplicate {show(ops[-1])}"
            elif op == "pop":
                note = f"discard {show(ops.pop())}"

            # --- objects and fields ---
            elif op == "new":
                cls_new = self.loader.load(arg)
                ref = self.heap.allocate(arg, cls_new.instance_fields)
                ops.append(ref); note = f"push reference {ref}"
            elif op == "getfield":
                owner, name, _ = split_ref(arg)
                obj = ops.pop()
                ops.append(self.heap.get_field(obj, name)); note = f"push {obj}.{name} = {show(ops[-1])}"
            elif op == "putfield":
                owner, name, _ = split_ref(arg)
                v, obj = ops.pop(), ops.pop()
                self.heap.put_field(obj, name, v); note = f"stored {show(v)} into {obj}.{name}"
            elif op == "getstatic":
                if arg.startswith("java/lang/System.out"):
                    ops.append(SYSTEM_OUT); note = "push System.out (the standard output stream)"
                else:
                    owner, name, _ = split_ref(arg)
                    ops.append(self.loader.load(owner).statics[name]); note = f"push static {owner}.{name}"
            elif op == "putstatic":
                owner, name, _ = split_ref(arg)
                self.loader.load(owner).statics[name] = ops.pop()
                note = f"static {owner}.{name} = {show(self.loader.load(owner).statics[name])}"

            # --- method calls ---
            elif op.startswith("invoke"):
                note = self.do_invoke(op, arg, ops)

            # --- returning ---
            elif op in ("ireturn", "areturn", "return"):
                result = ops.pop() if op != "return" else None
                if not t.brief:
                    t.detail(f"return {show(result)}" if op != "return" else "return (void)")
                self.stack.pop()
                return result
            else:
                raise RuntimeError(f"interpreter does not know {op}")

            if not t.brief:
                t.detail(f"{note}")
                t.detail(f"operands={show_list(ops)}  locals={show_list(frame.locals)}")
            i = index_of[jump] if jump is not None else i + 1

    def do_invoke(self, op: str, arg: str, ops: list) -> str:
        owner, name, desc = split_ref(arg)
        n = count_args(desc)
        args = [ops.pop() for _ in range(n)][::-1]

        # println / print: becomes a system call to the OS
        if owner == "java/io/PrintStream":
            ops.pop()   # System.out
            value = args[0] if args else ""
            if desc.startswith("(Z"):
                value = "true" if value else "false"
            text = f"{value}" + ("\n" if name == "println" else "")
            self.t.step("JVM", f"System.out.{name}({show(args[0]) if args else ''}) needs the screen "
                               "-> ask the OS")
            self.os.sys_write(self.proc, 1, text)
            return "printed"

        # java.lang.Object's constructor does nothing here
        if owner == "java/lang/Object":
            this = ops.pop()
            return f"Object.<init> for {this}: nothing to do"

        if op == "invokestatic":
            result = self.invoke(owner, name, args)
        else:
            this = ops.pop()
            if this is None:
                raise RuntimeError("NullPointerException")
            target = this.cls if op == "invokevirtual" else owner   # virtual dispatch uses the real class
            result = self.invoke(target, name, [this] + args)
        if not desc.endswith("V"):
            ops.append(result)
            return f"{owner}.{name} returned {show(result)}, pushed it"
        return f"{owner}.{name} finished"


# =====================================================================
# Running the whole thing
# =====================================================================

def compile_stage(source_path: str, disk: Disk, t: Tracer) -> str:
    t.stage(1, "COMPILE  (javac: Main.java -> bytecode)",
            "This happens once, before the program is run.")
    with open(source_path) as f:
        source = f.read()
    t.step("javac", f"read {os.path.basename(source_path)} ({len(source.splitlines())} lines)")
    tokens = tokenize(source)
    preview = " ".join(tok.value for tok in tokens[:12])
    t.step("javac", f"lexer: split the text into {len(tokens) - 1} tokens: {preview} ...")
    classes = Parser(tokens).parse_program()
    t.step("javac", f"parser: built a syntax tree for {len(classes)} classes: "
                    + ", ".join(c.name for c in classes))
    class_files = Compiler(classes).compile()
    for cf in class_files:
        n = sum(len(m.code) for m in cf.methods)
        t.step("javac", f"code generator: {cf.name} -> {len(cf.methods)} methods, {n} bytecode instructions")
        if not t.brief:
            t.box(f"{cf.name}.class (javap -c)", cf.disassemble().splitlines())
        disk.write(f"{cf.name}.class.json", json.dumps(cf.to_dict(), indent=2).encode())
    main_class = next((cf.name for cf in class_files
                       if any(m.name == "main" for m in cf.methods)), None)
    if main_class is None:
        raise CompileError("no class has a main method")
    return main_class


def run(source_path: str, t: Tracer, jit_threshold: int = 2):
    disk_folder = os.path.join(os.path.dirname(os.path.abspath(source_path)), "disk")
    devices = Devices(disk_folder, t)
    cpu = CPU(t)
    os_ = OS(devices, cpu, t)

    main_class = compile_stage(source_path, devices.disk, t)

    t.stage(2, "OPERATING SYSTEM starts the JVM process",
            f"The user runs: java {main_class}")
    proc = os_.spawn(f"java {main_class}")

    t.stage(3, "JVM BOOTS", "The JVM is just a program; now it builds its Heap, Stack and loaders.")
    jvm = JVM(os_, proc, t, jit_threshold)

    t.stage(4, "CLASS LOADING + EXECUTION",
            "Classes are loaded on demand. The interpreter runs bytecode; hot code gets JIT-compiled.")
    jvm.run_main(main_class)

    t.stage(5, "PROGRAM FINISHED")
    t.step("JVM", "main returned and no other threads are running -> shut down")
    t.box("final heap", jvm.heap.snapshot())
    os_.exit(proc, 0)

    print()
    print("=" * 78)
    print(" OUTPUT ON THE SCREEN")
    print("=" * 78)
    print(devices.console.text, end="")
    print()
    print(" Summary")
    print(f"   steps traced               : {t.n}")
    print(f"   bytecodes interpreted      : {cpu.bytecodes}")
    print(f"   machine instructions (JIT) : {cpu.native}")
    print(f"   methods JIT-compiled       : {', '.join(jvm.jit.compiled) or 'none'}")
    print(f"   objects on the heap        : {len(jvm.heap.objects)}")
    print(f"   deepest stack              : {jvm.stack.deepest} frames")
    print(f"   method calls               : "
          + ", ".join(f"{k} x{v}" for k, v in jvm.jit.calls.items()))


class Tee:
    """Sends everything printed to the screen AND to a file."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for s in self.streams:
            s.write(text)

    def flush(self):
        for s in self.streams:
            s.flush()


def main():
    p = argparse.ArgumentParser(description="Watch a Java program run step by step.")
    p.add_argument("source", nargs="?", default="Main.java")
    p.add_argument("--step", action="store_true", help="press Enter to advance each step")
    p.add_argument("--delay", type=float, default=0.0, help="seconds to wait between steps")
    p.add_argument("--brief", action="store_true", help="hide per-bytecode detail")
    p.add_argument("--jit-threshold", type=int, default=2, help="calls before a method is JIT-compiled")
    p.add_argument("--out", default="trace.txt", help="file to save the trace to (default: trace.txt)")
    p.add_argument("--quiet", action="store_true", help="only write to the file, not the screen")
    a = p.parse_args()

    screen = sys.stdout
    code = 0
    with open(a.out, "w", encoding="utf-8") as f:
        sys.stdout = f if a.quiet else Tee(screen, f)
        try:
            run(a.source, Tracer(a.step, a.delay, a.brief), a.jit_threshold)
        except CompileError as e:
            print(f"javac error: {e}")
            code = 1
        except RuntimeError as e:
            print(f"\nException in thread \"main\" {e}")
            code = 1
        finally:
            sys.stdout = screen
    print(f"\nTrace saved to {os.path.abspath(a.out)}")
    sys.exit(code)


if __name__ == "__main__":
    main()