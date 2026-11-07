// Export analyzed structure without treating inferred functions as matches.
// @category Decomp

import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.listing.Parameter;
import ghidra.program.model.mem.MemoryBlock;

public class GhidraExportInventory extends GhidraScript {
    @Override
    protected void run() throws Exception {
        String[] arguments = getScriptArgs();
        if (arguments.length != 1) {
            throw new IllegalArgumentException("expected an output JSON path");
        }

        JsonArray blocks = new JsonArray();
        for (MemoryBlock block : currentProgram.getMemory().getBlocks()) {
            JsonObject row = new JsonObject();
            row.addProperty("name", block.getName());
            row.addProperty("start", String.format("0x%08X", block.getStart().getOffset()));
            row.addProperty("size", block.getSize());
            row.addProperty("initialized", block.isInitialized());
            row.addProperty("executable", block.isExecute());
            blocks.add(row);
        }

        JsonArray functions = new JsonArray();
        FunctionIterator iterator = currentProgram.getFunctionManager().getFunctions(true);
        while (iterator.hasNext()) {
            Function function = iterator.next();
            List<String> calls = new ArrayList<>();
            for (Function target : function.getCalledFunctions(monitor)) {
                calls.add(String.format("0x%08X", target.getEntryPoint().getOffset()));
            }
            Collections.sort(calls);
            JsonArray callArray = new JsonArray();
            for (String address : calls) {
                callArray.add(address);
            }

            JsonObject row = new JsonObject();
            row.addProperty("address", String.format("0x%08X", function.getEntryPoint().getOffset()));
            row.addProperty("name", function.getName());
            row.addProperty("source", function.getSymbol().getSource().toString());
            row.addProperty("signature", function.getSignature().getPrototypeString());
            row.addProperty("signature_source", function.getSignatureSource().toString());
            row.addProperty("return_type", function.getReturnType().getDisplayName());
            row.addProperty("calling_convention", function.getCallingConventionName());
            row.addProperty("varargs", function.hasVarArgs());
            row.addProperty("no_return", function.hasNoReturn());
            row.addProperty("thunk", function.isThunk());
            Function thunkTarget = function.getThunkedFunction(false);
            if (thunkTarget != null) {
                row.addProperty("thunk_target", String.format("0x%08X", thunkTarget.getEntryPoint().getOffset()));
            }
            JsonArray parameters = new JsonArray();
            for (Parameter parameter : function.getParameters()) {
                JsonObject item = new JsonObject();
                item.addProperty("ordinal", parameter.getOrdinal());
                item.addProperty("name", parameter.getName());
                item.addProperty("type", parameter.getDataType().getDisplayName());
                item.addProperty("source", parameter.getSource().toString());
                parameters.add(item);
            }
            row.add("parameters", parameters);
            row.addProperty("body_min", String.format("0x%08X", function.getBody().getMinAddress().getOffset()));
            row.addProperty("body_max", String.format("0x%08X", function.getBody().getMaxAddress().getOffset()));
            row.addProperty("body_bytes", function.getBody().getNumAddresses());
            row.add("direct_calls", callArray);
            functions.add(row);
        }

        JsonObject inventory = new JsonObject();
        inventory.addProperty("generated_at", Instant.now().toString());
        inventory.addProperty("program", currentProgram.getName());
        inventory.addProperty("input_sha256", currentProgram.getExecutableSHA256());
        inventory.addProperty("language", currentProgram.getLanguageID().toString());
        inventory.addProperty("compiler_spec", currentProgram.getCompilerSpec().getCompilerSpecID().toString());
        inventory.addProperty("schema_version", 2);
        inventory.add("memory_blocks", blocks);
        inventory.addProperty("function_count", functions.size());
        inventory.add("functions", functions);
        inventory.addProperty("scope", "Ghidra auto-analysis; names, boundaries, and calls require independent verification");
        try (Writer output = Files.newBufferedWriter(Paths.get(arguments[0]), StandardCharsets.UTF_8)) {
            new GsonBuilder().setPrettyPrinting().create().toJson(inventory, output);
        }
        println("Exported " + functions.size() + " analyzed functions to " + arguments[0]);
    }
}
