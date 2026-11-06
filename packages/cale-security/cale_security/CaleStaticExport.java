// Cale-owned static exporter. No source from the analyzed program is executed.
// @category Cale
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import ghidra.app.util.headless.HeadlessScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;

public class CaleStaticExport extends HeadlessScript {
    @Override public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length != 1 || currentProgram == null) {
            throw new IllegalArgumentException("One host-owned report path and program required");
        }
        JsonObject report = new JsonObject();
        report.addProperty("schema", 1);
        report.addProperty("input_sha256", currentProgram.getExecutableSHA256());
        report.addProperty("format", currentProgram.getExecutableFormat());
        report.addProperty("language", currentProgram.getLanguageID().toString());
        report.addProperty("analysis_timed_out", analysisTimeoutOccurred());
        report.addProperty("function_count", currentProgram.getFunctionManager().getFunctionCount());
        JsonArray libraries = new JsonArray();
        for (String name : currentProgram.getExternalManager().getExternalLibraryNames()) {
            if (libraries.size() >= 256) break;
            libraries.add(name.length() > 512 ? name.substring(0, 512) : name);
        }
        report.add("external_libraries", libraries);
        JsonArray functions = new JsonArray();
        DecompInterface decompiler = new DecompInterface();
        try {
            if (!decompiler.openProgram(currentProgram)) {
                throw new IllegalStateException("Decompiler could not open imported program");
            }
            FunctionIterator iterator = currentProgram.getFunctionManager().getFunctions(true);
            while (iterator.hasNext() && functions.size() < 64) {
                monitor.checkCancelled();
                Function function = iterator.next();
                JsonObject entry = new JsonObject();
                String name = function.getName();
                entry.addProperty("name", name.length() > 512 ? name.substring(0, 512) : name);
                entry.addProperty("address", function.getEntryPoint().toString());
                entry.addProperty("external", function.isExternal());
                DecompileResults result = decompiler.decompileFunction(function, 2, monitor);
                if (result.decompileCompleted() && result.getDecompiledFunction() != null) {
                    String code = result.getDecompiledFunction().getC();
                    entry.addProperty("decompilation", code.length() > 12000 ? code.substring(0, 12000) : code);
                    entry.addProperty("clipped", code.length() > 12000);
                } else {
                    entry.addProperty("decompilation_unavailable", true);
                }
                functions.add(entry);
            }
        } finally { decompiler.dispose(); }
        report.addProperty("exported_functions", functions.size());
        report.addProperty("functions_limited", currentProgram.getFunctionManager().getFunctionCount() > functions.size());
        report.add("functions", functions);
        byte[] bytes = report.toString().getBytes(StandardCharsets.UTF_8);
        if (bytes.length > 2 * 1024 * 1024) {
            throw new IllegalStateException("Static export exceeded host byte limit");
        }
        Files.write(Path.of(args[0]), bytes, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE);
    }
}
