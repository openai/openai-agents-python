"""Stock named XPC fixture metadata and bounded LLDB inspection."""

from collections import deque
import hashlib
from pathlib import Path
import re
import sys


SOURCE_SHA256 = "be1af7b3622e9e70bc261a1d3773c7facb96666e1a8217e3393bd657a2f73dd9"
STOCK_UUID = "D3682603-AC67-3242-82EB-1226B435BFF4"
OLD_RAW_RESULT = """    printf("raw class=%s id=%" PRId32 " descriptors=%" PRIu32 "\\n",
           complex ? "complex" : "simple", (int32_t)header->msgh_id, (uint32_t)descriptors);
    ok = true;
"""
NAMED_RESULT = """    if (complex || header->msgh_id != 0x40000324 || descriptors != 0) {
        failure("named-request-shape", header->msgh_id);
        goto cleanup;
    }
    unsigned int name_matches = 0;
    for (size_t offset = sizeof(*header); offset + sizeof(allowed) - 1 <= size; ++offset) {
        if (memcmp(msg.bytes + offset, allowed, sizeof(allowed) - 1) == 0) {
            ++name_matches;
        }
    }
    mach_port_type_t reply_type = 0;
    if (MACH_PORT_VALID(header->msgh_remote_port)) {
        kr = mach_port_type(mach_task_self(), header->msgh_remote_port, &reply_type);
        if (kr != KERN_SUCCESS) {
            failure("named-reply-right-type", kr);
            goto cleanup;
        }
    }
    printf("named_request=authenticated id=0x%" PRIx32 " size=%zu descriptors=0"
           " fixed_name_matches=%u reply_disposition=%u reply_right_type=0x%x"
           " reply_send_once=%s reply_sent=false\\n",
           (uint32_t)header->msgh_id, size, name_matches,
           (unsigned int)MACH_MSGH_BITS_REMOTE(header->msgh_bits), (unsigned int)reply_type,
           (reply_type & MACH_PORT_TYPE_SEND_ONCE) != 0 ? "true" : "false");
    ok = true;
"""


def prepare(source_path, output_path):
    raw = Path(source_path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise SystemExit("unexpected stock fixture source")
    source = raw.decode()
    if source.count(OLD_RAW_RESULT) != 1:
        raise SystemExit("stock raw receipt anchor mismatch")
    Path(output_path).write_text(source.replace(OLD_RAW_RESULT, NAMED_RESULT))


def inspect(debugger, command, result, internal_dict):
    import lldb

    target = debugger.GetSelectedTarget()
    process = target.GetProcess()
    frame = process.GetSelectedThread().GetFrameAtIndex(0)
    if (
        process.GetState() != lldb.eStateStopped
        or frame.GetFunctionName() != "main"
        or frame.GetModule().GetFileSpec() != target.GetExecutable()
    ):
        result.SetError("expected the owned executable stopped at main")
        return

    module = target.FindModule(lldb.SBFileSpec("/usr/lib/system/libxpc.dylib"))
    if (
        not module.IsValid()
        or module.GetUUIDString() != STOCK_UUID
        or not module.GetTriple().startswith("arm64")
    ):
        result.SetError("unexpected loaded libxpc identity")
        return
    result.AppendMessage(f"loaded_libxpc={module.GetUUIDString()} triple={module.GetTriple()}")

    roots = (
        "xpc_connection_create_mach_service",
        "xpc_connection_resume",
        "xpc_connection_activate",
        "_xpc_pipe_interface_routine",
        "_xpc_pipe_interface_routine_async",
        "xpc_pipe_routine_with_flags",
    )
    pending = deque()
    for name in roots:
        symbol = module.FindSymbol(name, lldb.eSymbolTypeCode)
        if symbol.IsValid():
            pending.append((symbol, 0))
        else:
            result.AppendMessage(f"root={name} unavailable")

    # A stock dispatch closure may be referenced as data. Only seed symbols
    # whose names identify the named connection lookup or its reply.
    named_symbols = []
    for index in range(min(module.GetNumSymbols(), 12000)):
        symbol = module.GetSymbolAtIndex(index)
        name = symbol.GetName() or ""
        if symbol.GetType() == lldb.eSymbolTypeCode and "xpc_connection" in name and any(
            part in name for part in ("look_up", "lookup", "bootstrap", "resolve")
        ):
            named_symbols.append((symbol, 0))
    pending.extend(named_symbols[:6])
    result.AppendMessage(f"named_symbol_candidates={len(named_symbols)} included={min(6, len(named_symbols))}")

    seen = set()
    maximum_symbols = 22
    remaining_instructions = 1600
    target_instruction = re.compile(r"(?:0x40000324|#0x324|#804)(?![0-9a-f])", re.IGNORECASE)
    branch_address = re.compile(r"#?(0x[0-9a-f]+)(?:\s|$)", re.IGNORECASE)
    while pending and len(seen) < maximum_symbols and remaining_instructions:
        symbol, depth = pending.popleft()
        start = symbol.GetStartAddress()
        end = symbol.GetEndAddress()
        if not start.IsValid() or not end.IsValid() or start.GetSection() != end.GetSection():
            continue
        address = start.GetFileAddress()
        size = end.GetFileAddress() - address
        if address in seen or size < 4 or size % 4:
            continue
        # Put a cap on large or unexpectedly coalesced symbols.
        instructions = target.ReadInstructions(start, min(size // 4, 144, remaining_instructions))
        count = instructions.GetSize()
        if not count:
            continue
        remaining_instructions -= count
        seen.add(address)
        name = symbol.GetName()
        result.AppendMessage(
            f"symbol={name} depth={depth} size={size} inspected={count} complete={count == size // 4}"
        )
        for instruction in instructions:
            offset = instruction.GetAddress().GetFileAddress() - address
            mnemonic = instruction.GetMnemonic(target) or ""
            operands = instruction.GetOperands(target) or ""
            comment = instruction.GetComment(target) or ""
            if target_instruction.search(operands):
                result.AppendMessage(f"lookup_literal_candidate={name}+0x{offset:x}")
            result.AppendMessage(f"  +0x{offset:x} {mnemonic} {operands} {comment}".rstrip())
            branch = branch_address.match(operands)
            if depth >= 2 or mnemonic not in {"b", "bl"} or branch is None:
                continue
            destination = target.ResolveLoadAddress(int(branch[1], 16))
            if destination.IsValid() and destination.GetModule() == module:
                callee = destination.GetSymbol()
                if callee.IsValid() and callee.GetType() == lldb.eSymbolTypeCode:
                    pending.append((callee, depth + 1))

    if not seen:
        result.SetError("no bounded libxpc code available")
        return
    result.AppendMessage(
        f"static_symbols={len(seen)} pending={len(pending)} dynamic_calls=false "
        "parser_contract=manual_review_required"
    )


def __lldb_init_module(debugger, internal_dict):
    debugger.HandleCommand("command script add -f stock_xpc_named_parser.inspect inspect-named-xpc")


if __name__ == "__main__":
    prepare(sys.argv[1], sys.argv[2])
