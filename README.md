# From Source to Silicon

**A step-by-step simulator that shows what really happens when a computer runs a Java program.**

When you run a Java program, a lot happens that you never see. The code is compiled, an operating system starts a process, a virtual machine loads classes, memory is used on the stack and the heap, hot code is turned into machine code, and finally a device puts the answer on the screen.

This project makes all of that visible. It contains a mini Java compiler and a simulated computer, both written in Python, that run a real Java program and narrate every step from source code to output.

```
Main.java ─► javac ─► bytecode ─► OS starts JVM ─► ClassLoader ─► Interpreter ─► JIT ─► CPU ─► Screen
                                                       │              │
                                                     Heap           Stack
```

---

## Files

| File | What it is |
|------|------------|
| `mini_javac.py` | A tiny Java compiler. Turns Java source code into JVM bytecode. |
| `jvm_sim.py` | The simulator: OS, devices, CPU, JVM, class loader, interpreter, heap, stack and JIT. |
| `Main.java` | A small example program to run through the simulator. |
| `trace.txt` | Created when you run the simulator: the full step-by-step trace. |

## Requirements

- Python 3.10 or newer
- No extra libraries are needed

You don't need Java installed. The project includes its own compiler and JVM.

---

## Quick start

```bash
python jvm_sim.py Main.java
```

This compiles `Main.java`, runs it on the simulated computer, prints every step, and saves the trace to `trace.txt`.

### Options

| Command | What it does |
|---------|--------------|
| `python jvm_sim.py Main.java` | Run and show the full trace (also saved to `trace.txt`) |
| `python jvm_sim.py Main.java --step` | Press Enter to move forward one step at a time |
| `python jvm_sim.py Main.java --delay 0.3` | Auto-play with a short pause between steps |
| `python jvm_sim.py Main.java --brief` | Hide the per-bytecode detail and show just the big picture |
| `python jvm_sim.py Main.java --out run.txt` | Save the trace to a different file |
| `python jvm_sim.py Main.java --quiet` | Write only to the file, not the screen |
| `python jvm_sim.py Main.java --jit-threshold 3` | Change how many calls make a method "hot" |

You can also use the compiler on its own, which prints a `javap`-style listing of the bytecode:

```bash
python mini_javac.py Main.java
```

---

## The example program

```java
public class Main {
    static int square(int n) {
        return n * n;
    }

    public static void main(String[] args) {
        Point p = new Point(3, 4);
        int total = 0;
        for (int i = 1; i <= 3; i++) {
            total += square(i);
        }
        System.out.println(total + p.sum());
    }
}

class Point {
    int x;
    int y;

    Point(int x, int y) {
        this.x = x;
        this.y = y;
    }

    int sum() {
        return x + y;
    }
}
```

It computes `1² + 2² + 3² = 14`, adds `3 + 4 = 7`, and prints **21**.

This small program still exercises every part of the system. It creates an object on the heap, calls methods that push frames onto the stack, and calls `square` often enough for the JIT to compile it.

---

## What happens, stage by stage

### Stage 1: Compile (`javac`)
The mini compiler reads `Main.java` and works the way the real `javac` does:

1. **Lexer**: splits the text into tokens (`public`, `class`, `Main`, `{`, ...).
2. **Parser**: builds a syntax tree from the tokens.
3. **Code generator**: walks the tree and produces JVM bytecode (`iload`, `imul`, `invokestatic`, ...).

The class files are written to the simulated **Disk**.

### Stage 2: The OS starts the JVM
The user runs `java Main`. The **OS** creates a process (PID 4242), gives it a memory map with code, heap and stack regions, and schedules it on a CPU core.

### Stage 3: The JVM boots
The JVM is just a program running inside that process. It creates its runtime areas: the **Heap**, the main thread's **Stack**, the **ClassLoader**, the **Interpreter** and the **JIT**.

### Stage 4: Class loading and execution
- The **ClassLoader** loads each class the first time it's needed. For example, `Point` is only loaded when `new Point` runs. It reads the file with a `read()` system call to the OS, which asks the Disk. Then it **verifies**, **links** and **initializes** the class.
- The **Interpreter** runs the bytecode one instruction at a time and shows the operand stack and local variables after each one.
- Each method call **pushes a frame** onto the **Stack**, and each return **pops** it.
- `new Point(3, 4)` **allocates an object on the Heap**, and the constructor fills in its fields.
- After `square` has been called twice, the **JIT** marks it as hot and compiles it into x86-style machine code:

  ```
  iload_0   =>  mov eax, edi
  iload_0   =>  mov r8d, edi
  imul      =>  imul eax, r8d
  ireturn   =>  ret
  ```

  The third call skips the interpreter and runs directly on the **CPU**, and you can watch the register values change.

### Stage 5: Output and exit
`System.out.println(21)` becomes a `write()` **system call**. The OS sends the text to the **Console** device, and `21` appears on the screen. The process then exits and its memory is freed. A summary shows how many bytecodes were interpreted, how many machine instructions ran, what was JIT-compiled, and how deep the stack got.

---

## Sample output (trimmed)

```
[028] Heap        | allocated a Point object at address 0x1000 (x=0, y=0, defaults)
...
[040] JIT         | Main.square has been called 2 times -> it is HOT, compile it to machine code
...
[044] CPU         | jumps straight into machine code for Main.square (edi=3)
                  |   mov eax, edi      ; eax = 3
                  |   mov r8d, edi      ; r8d = 3
                  |   imul eax, r8d     ; eax = 9
                  |   ret               ; return value is in eax = 9
...
[050] OS          | system call write(fd=1 stdout, "21\n") from PID 4242 -> sending to the Console
[051] Console     | screen shows: 21
```

(These step numbers come from a `--brief` run. A full run has more steps because every bytecode instruction gets its own.)

---

## How the code is organised

### `mini_javac.py`

| Part | Role |
|------|------|
| `tokenize()` | Lexer: source text → tokens |
| `Parser` | Recursive-descent parser: tokens → syntax tree |
| `Compiler` / `MethodGen` | Code generator: syntax tree → bytecode, including jump targets, `max_stack` and `max_locals` |
| `ClassFile` | The output, saved as `.class.json` and printable like `javap -c` |
| `Javac` | Ties the stages together |

### `jvm_sim.py`

| Class | Role |
|-------|------|
| `Disk`, `Console`, `Devices` | Hardware: storage for class files and a screen for output |
| `CPU` | Executes JIT-compiled machine code using registers |
| `OS`, `Process` | Creates the process, sets up memory, handles `read` and `write` system calls |
| `Heap` | Allocates objects and stores their fields |
| `Stack`, `Frame` | One frame per running method, holding its local variables and operand stack |
| `ClassLoader`, `LoadedClass` | Loads, verifies, links and initializes classes |
| `JIT` | Counts calls, finds hot methods and translates bytecode into machine code |
| `JVM` | Boots the runtime, dispatches method calls and contains the bytecode interpreter |
| `Tracer` | Prints and numbers every step, and handles step-by-step and delay modes |

---

## Supported Java

The compiler handles a small but useful subset of Java:

- Classes with `int`, `boolean` and object fields, both static and instance
- Methods and constructors with parameters
- Local variables, `=`, `+=`, `-=`, `*=`, `/=`, `%=`, `++` and `--`
- Arithmetic `+ - * / %`, comparisons `< <= > >= == !=`, and logic `&& || !`
- `if` / `else`, `while`, `for` and `return`
- Method calls, `new`, field access and `this`
- `System.out.println` and `System.out.print` with an `int`, `boolean` or string literal

## Simplifications

This is a teaching model, not a production JVM. Some things are deliberately simpler than the real thing:

- **Class files** are saved as readable JSON instead of the binary `.class` format, and there is no constant pool.
- **The JIT** only compiles static methods that do simple `int` arithmetic. Other methods stay interpreted. Its machine code is x86-style assembly that runs on the simulated CPU, not on your real processor.
- **Memory** addresses, the memory map and the process ID are illustrative.
- **Not supported:** arrays (other than `String[] args`), strings beyond literals, inheritance, interfaces, exceptions, threads and garbage collection.

## Ideas for extending it

- A garbage collector that shows unreachable objects being freed from the heap
- Arrays and string objects on the heap
- A JIT that handles loops and field access
- A visual or web front end that animates the stack and heap
- Multiple threads, each with its own stack, scheduled by the OS
