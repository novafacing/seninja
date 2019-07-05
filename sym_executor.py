import z3
from binaryninja import (
    BinaryReader, BinaryWriter, 
    RegisterValueType, enums
)
from sym_state import State
from arch.arch_x86 import x86Arch
from arch.arch_x86_64 import x8664Arch
from models.function_models import library_functions
from utility.z3_wrap_util import (
    bvv_from_bytes, bvv, bvs, symbolic
)
from utility.bninja_util import (
    get_function, get_imported_functions, 
    get_imported_addresses
)
from memory.sym_memory import InitData
from multipath.fringe import Fringe

NO_COLOR             = enums.HighlightStandardColor(0)
CURR_STATE_COLOR     = enums.HighlightStandardColor.GreenHighlightColor
DEFERRED_STATE_COLOR = enums.HighlightStandardColor.RedHighlightColor

class BNILVisitor(object):
    # thanks joshwatson (https://github.com/joshwatson/f-ing-around-with-binaryninja/blob/master/ep4-emulator/vm_visitor.py)
    def __init__(self, **kw):
        super(BNILVisitor, self).__init__()

    def visit(self, expression):
        method_name = 'visit_{}'.format(expression.operation.name)
        if hasattr(self, method_name):
            value = getattr(self, method_name)(expression)
        else:
            value = None
        return value

class SymbolicVisitor(BNILVisitor):
    def __init__(self, view, addr):
        super(SymbolicVisitor, self).__init__()

        self.view    = view
        self.bw      = BinaryWriter(view)
        self.br      = BinaryReader(view)
        self.vars    = set()
        self.fringe  = Fringe()
        self.ip      = addr
        self.llil_ip = None 
        self.arch    = None
        self.imported_functions = get_imported_functions(view)
        self.imported_addresses = get_imported_addresses(view)

        self._wasjmp = False
        if self.view.arch.name == "x86":
            self.arch = x86Arch()
        elif self.view.arch.name == "x86_64":
            self.arch = x8664Arch()
        
        assert self.arch is not None
        self.state = State(self, arch=self.arch, page_size=0x1000)

        # load memory
        print("\nloading segments...") 
        for segment in self.view.segments:
            start = segment.start
            size  = segment.data_length

            if size == 0:
                continue
            
            self.br.seek(start)
            data = self.br.read(size)

            self.state.mem.mmap(
                self.state.address_page_aligned(start),
                self.state.address_page_aligned(start + size + self.state.mem.page_size) - self.state.address_page_aligned(start),
                InitData(data, start - self.state.address_page_aligned(start))
            )

        print("segment loading finished.\n")

        current_function = get_function(view, addr)

        # initialize stack
        unmapped_page_init = self.state.get_unmapped(2)
        self.state.mem.mmap(unmapped_page_init*self.state.page_size, self.state.page_size * 2)
        p = unmapped_page_init + 1
        stack_base = p * self.state.page_size - self.arch.bits() // 8

        self.state.initialize_stack(stack_base)

        # initialize registers
        for reg in self.arch.regs_data():
            val = current_function.get_reg_value_after(addr, reg)

            if val.type.value == RegisterValueType.StackFrameOffset:
                setattr(self.state.regs, reg, bvv(stack_base + val.offset, self.arch.bits()))
            elif (
                val.type.value == RegisterValueType.ConstantPointerValue or 
                val.type.value == RegisterValueType.ConstantValue
            ):
                setattr(self.state.regs, reg, bvv(val.value, self.arch.bits()))
            else:
                symb = bvs(reg + "_init", self.arch.bits())
                self.vars.add(symb)
                setattr(self.state.regs, reg, symb)
        
        # initialize known local variables
        stack_vars = current_function.stack_layout
        for var in stack_vars:
            offset = var.storage
            s_type = var.type

            if s_type.confidence != 255:
                continue
            
            width = s_type.width
            name = var.name
            val  = current_function.get_stack_contents_at(addr, offset, width)
            if val.type.value == RegisterValueType.StackFrameOffset:
                assert width*8 == self.arch.bits()  # has to happen... right?
                self.state.mem.store(
                    bvv(stack_base + offset, self.arch.bits()), 
                    bvv(stack_base + val.offset, width*8 ))
            elif (
                val.type.value == RegisterValueType.ConstantPointerValue or 
                val.type.value == RegisterValueType.ConstantValue
            ):
                self.state.mem.store(
                    bvv(stack_base + offset, self.arch.bits()), 
                    bvv(val.value, width*8 ))
            else:
                symb = bvs(name + "_init", self.arch.bits())
                self.vars.add(symb)
                self.state.mem.store(
                    bvv(stack_base + offset, self.arch.bits()), 
                    symb )
        
        # set eip
        self.state.set_ip(addr)
        self.llil_ip = current_function.llil.get_instruction_start(addr)

        current_function.set_auto_instr_highlight(self.ip, CURR_STATE_COLOR)
    
    def _check_unsupported(self, val, expr):
        if val is None:
            raise Exception("unsupported instruction '%s'" % (expr.operation.name))
    
    def _handle_symbolic_ip(self):
        raise NotImplementedError  # implement this
    
    def _put_in_deferred(self, state):
        ip = state.get_ip()
        self.fringe.add_deferred(state)

        func = get_function(self.view, ip)
        func.set_auto_instr_highlight(ip, DEFERRED_STATE_COLOR)
    
    def set_current_state(self, state):
        if self.state is not None:
            self._put_in_deferred(self.state)
            self.state = None
        
        ip = state.get_ip()

        self.state = state
        new_func = get_function(self.view, ip) 
        self.ip = ip
        self.llil_ip = new_func.llil.get_instruction_start(ip)

        new_func.set_auto_instr_highlight(self.ip, CURR_STATE_COLOR)

    def select_from_deferred(self):
        if self.fringe.is_empty():
            return False
        
        state = self.fringe.get_one_deferred()
        self.set_current_state(state)
        return True
    
    def update_ip(self, function, new_llil_ip):
        old_ip = self.ip
        old_func = get_function(self.view, old_ip)

        self.llil_ip = new_llil_ip
        self.ip = function.llil[new_llil_ip].address
        self.state.set_ip(self.ip)

        if old_ip in self.fringe.deferred_addresses:
            old_func.set_auto_instr_highlight(old_ip, DEFERRED_STATE_COLOR)
        else:
            old_func.set_auto_instr_highlight(old_ip, NO_COLOR)
        function.set_auto_instr_highlight(self.ip, CURR_STATE_COLOR)
    
    def execute_one(self):
        func = get_function(self.view, self.ip)
        expr = func.llil[self.llil_ip]
        res = self.visit(expr)

        self._check_unsupported(res, expr)
        
        if self.state is None:
            if self.fringe.is_empty():
                return
            else:
                self.select_from_deferred()
                self._wasjmp = False
        
        if not self._wasjmp:
            # go on by 1 instruction
            self.update_ip(func, self.llil_ip + 1)
        else:
            self._wasjmp = False

    def visit_LLIL_STORE(self, expr):
        dest = self.visit(expr.dest)
        src  = self.visit(expr.src)

        self._check_unsupported(dest, expr.dest)
        self._check_unsupported(src,  expr.src )

        self.state.mem.store(dest, src, endness=self.arch.endness())
        return True

    def visit_LLIL_CONST(self, expr):
        return bvv(expr.constant, expr.size * 8)

    def visit_LLIL_CONST_PTR(self, expr):
        return bvv(expr.constant, self.arch.bits())

    def visit_LLIL_SET_REG(self, expr):
        dest = expr.dest.name
        src  = self.visit(expr.src)

        self._check_unsupported(src, expr.src)

        setattr(self.state.regs, dest, src)
        return True
    
    def visit_LLIL_ADD(self, expr):
        left  = self.visit(expr.left)
        right = self.visit(expr.right)

        self._check_unsupported(left,  expr.left )
        self._check_unsupported(right, expr.right)
        
        return z3.simplify(left + right)

    def visit_LLIL_SUB(self, expr):
        left  = self.visit(expr.left)
        right = self.visit(expr.right)

        self._check_unsupported(left,  expr.left )
        self._check_unsupported(right, expr.right)
        
        return z3.simplify(left - right)

    def visit_LLIL_LOAD(self, expr):
        src = self.visit(expr.src)

        self._check_unsupported(src, expr.src)
        
        loaded = self.state.mem.load(src, expr.size, endness=self.arch.endness())

        return loaded

    def visit_LLIL_XOR(self, expr):
        left = self.visit(expr.left)
        right = self.visit(expr.right)

        self._check_unsupported(left,  expr.left )
        self._check_unsupported(right, expr.right)

        return z3.simplify(left ^ right)

    def visit_LLIL_LSL(self, expr):
        left = self.visit(expr.left)
        right = self.visit(expr.right)

        assert right.size() < left.size()

        self._check_unsupported(left,  expr.left )
        self._check_unsupported(right, expr.right)

        # the logical and arithmetic left-shifts are exactly the same
        return z3.simplify(left << z3.ZeroExt(left.size() - right.size(), right))

    def visit_LLIL_REG(self, expr):
        src = expr.src
        return getattr(self.state.regs, src.name)
    
    def visit_LLIL_PUSH(self, expr):
        src = self.visit(expr.src)

        self._check_unsupported(src, expr.src)
        
        self.state.stack_push(src)
        return True

    def visit_LLIL_CALL(self, expr):
        dest = self.visit(expr.dest)

        self._check_unsupported(dest, expr.dest)
        
        if symbolic(dest):
            raise Exception("symbolic IP")
        
        curr_fun = get_function(self.view, self.ip)
        dest_fun = self.view.get_function_at(dest.as_long())
        ret_addr = curr_fun.llil[self.llil_ip + 1].address

        # push ret address
        self.state.stack_push(bvv(ret_addr, self.arch.bits()))

        # check if imported
        if dest.as_long() in self.imported_functions:
            name = self.imported_functions[dest.as_long()]
            if name not in library_functions:
                raise Exception("unsupported external function '%s'" % name)
            
            res = library_functions[name](self.state)
            setattr(self.state.regs, self.arch.get_result_register(res.size()), res)
            
            dest = self.state.stack_pop()
            dest_fun = curr_fun 
            assert not symbolic(dest)  # cannot happen (right?)

        # change ip
        self.update_ip(dest_fun, dest_fun.llil.get_instruction_start(dest.as_long()))

        self._wasjmp = True
        return True
    
    def visit_LLIL_POP(self, expr):
        return self.state.stack_pop()
    
    def visit_LLIL_IF(self, expr):
        condition = self.visit(expr.condition)
        true_llil_index = expr.true
        false_llil_index = expr.false

        self._check_unsupported(condition, expr.condition)
        
        curr_fun = get_function(self.view, self.ip)
        false_state = self.state.copy()

        self.state.solver.add_constraints(condition)

        if self.state.solver.satisfiable():
            self.update_ip(curr_fun, true_llil_index)
        else:
            self.fringe.unsat.append(self.state)
            self.state = None

        false_state.solver.add_constraints(z3.Not(condition))
        if false_state.solver.satisfiable():
            false_state.set_ip(curr_fun.llil[false_llil_index].address)
            if self.state is None:
                self.state = false_state
            else:
                self._put_in_deferred(false_state)
        else:
            self.fringe.unsat.append(false_state)

        self._wasjmp = True
        return True
    
    def visit_LLIL_CMP_NE(self, expr):
        left = self.visit(expr.left)
        right = self.visit(expr.right)

        self._check_unsupported(left,  expr.left )
        self._check_unsupported(right, expr.right)
        
        return left != right
    
    def visit_LLIL_GOTO(self, expr):
        dest = expr.dest

        curr_fun = get_function(self.view, self.ip)
        self.update_ip(curr_fun, dest)
        
        self._wasjmp = True
        return True

    def visit_LLIL_RET(self, expr):
        dest = self.visit(expr.dest)

        if symbolic(dest):
            raise Exception("symbolic IP")
        
        dest_fun = self.view.get_function_at(dest.as_long())
        self.update_ip(dest_fun, dest_fun.llil.get_instruction_start(dest.as_long()))

        self._wasjmp = True
        return True

    # def visit_LLIL_NORET(self, expr):
    #     log_alert("VM Halted.")