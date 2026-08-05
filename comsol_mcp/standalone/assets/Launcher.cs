using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

internal static class ComsolMcpStandaloneLauncher
{
    private const string TemplateResource = "CapacitorPointTemplate.java";
    private const string EventPrefix = "COMSOL_MCP_EVENT ";
    private const int MaximumDocumentBytes = 1024 * 1024;
    private const int MaximumLogBytes = 4 * 1024 * 1024;
    private const int MaximumTailLines = 500;
    private const int CompileTimeoutMilliseconds = 60000;
    private const int PointTimeoutMilliseconds = 600000;
    private const int MaximumAttempts = 8;
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    private static readonly PointSpec[] Points = new PointSpec[]
    {
        new PointSpec(1, "voltage_1V", "1.0"),
        new PointSpec(2, "voltage_2V", "2.0"),
        new PointSpec(3, "voltage_3V", "3.0")
    };
    private static IntPtr CampaignJob = IntPtr.Zero;
    private static ExecutionIdentity ActiveIdentity = null;

    private static int Main(string[] args)
    {
        try
        {
            Json.MaxJsonLength = MaximumDocumentBytes;
            if (args.Length == 0)
            {
                throw new ArgumentException(Usage());
            }
            string command = args[0].ToLowerInvariant();
            if (command == "run" || command == "resume")
            {
                string comsolRoot = ParseComsolPath(args, 1);
                return RunCampaign(command == "resume", comsolRoot);
            }
            if (command == "status" && args.Length == 1)
            {
                return PrintStatus();
            }
            if (command == "pause" && args.Length == 1)
            {
                return RequestPause();
            }
            if (command == "results" && args.Length == 1)
            {
                return PrintResults();
            }
            if (command == "tail")
            {
                return PrintTail(args);
            }
            throw new ArgumentException(Usage());
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine("COMSOL MCP standalone launcher failed: " + exception);
            return 1;
        }
    }

    private static string Usage()
    {
        return "usage: comsol-mcp-standalone.exe "
                + "{run|resume --comsol-path <COMSOL-root>|status|pause|results|tail [lines]}";
    }

    private static int RunCampaign(bool resume, string comsolRoot)
    {
        ValidateHost(comsolRoot);
        ExecutionIdentity identity = ExecutionIdentity.Create(comsolRoot);
        ActiveIdentity = identity;
        Paths paths = new Paths(ExecutableDirectory());
        paths.Create();
        using (FileStream ownerLock = AcquireOwnerLock(paths))
        {
            bool attemptStarted = false;
            int committedCount = 0;
            try
            {
                EnterCampaignJob();
                RejectExternalComsolOwners();
                Thread.Sleep(250);
                RejectExternalComsolOwners();

                List<Dictionary<string, object>> existing = ReadAndValidateResults(
                    paths, identity
                );
                committedCount = existing.Count;
                if (!resume && existing.Count != 0)
                {
                    throw new InvalidOperationException("fresh_run_refuses_existing_results");
                }
                if (resume && !CanResume(paths, identity))
                {
                    throw new InvalidOperationException("campaign_state_is_not_resumable");
                }
                RequireAttemptBudget(paths);

                string attemptId = Guid.NewGuid().ToString("N");
                WriteOwner(paths, attemptId, comsolRoot);
                WriteStatus(paths, Status(
                    "running", attemptId, existing.Count, "preflight", null, null
                ));
                attemptStarted = true;
                Log(paths, "attempt_started", attemptId);

                if (existing.Count == Points.Length)
                {
                    Dictionary<string, object> recoveredSummary = ValidateCampaignPhysics(
                        existing
                    );
                    Dictionary<string, object> recovered = Status(
                        "completed", attemptId, existing.Count, "terminal", null, null
                    );
                    recovered["physical_summary"] = recoveredSummary;
                    recovered["results_sha256"] = Sha256(paths.Results);
                    recovered["recovered_without_rerun"] = true;
                    WriteTerminalStatus(paths, recovered);
                    Log(paths, "campaign_recovered_completed", attemptId);
                    Console.WriteLine(Json.Serialize(recovered));
                    return 0;
                }

                for (int index = existing.Count; index < Points.Length; index++)
                {
                    PointSpec point = Points[index];
                    if (existing.Count < Points.Length && PauseRequested(paths, attemptId))
                    {
                        AcknowledgePause(paths, attemptId, existing.Count);
                        return 0;
                    }
                    Dictionary<string, object> result = RunPoint(
                        paths, comsolRoot, attemptId, point, existing.Count, identity
                    );
                    AppendResult(paths.Results, result);
                    existing.Add(result);
                    committedCount = existing.Count;
                    WriteStatus(paths, Status(
                        "running",
                        attemptId,
                        existing.Count,
                        "point_committed",
                        point.Id,
                        null
                    ));
                    Log(paths, "point_committed", point.Id);
                    if (PauseRequested(paths, attemptId))
                    {
                        AcknowledgePause(paths, attemptId, existing.Count);
                        return 0;
                    }
                }

                Dictionary<string, object> physicalSummary = ValidateCampaignPhysics(existing);
                Dictionary<string, object> completed = Status(
                    "completed", attemptId, existing.Count, "terminal", null, null
                );
                completed["physical_summary"] = physicalSummary;
                completed["results_sha256"] = Sha256(paths.Results);
                WriteTerminalStatus(paths, completed);
                Log(paths, "campaign_completed", attemptId);
                Console.WriteLine(Json.Serialize(completed));
                return 0;
            }
            catch (Exception exception)
            {
                if (attemptStarted)
                {
                    TryWriteFailureStatus(exception.Message, committedCount);
                }
                throw;
            }
        }
    }

    private static Dictionary<string, object> RunPoint(
        Paths paths,
        string comsolRoot,
        string attemptId,
        PointSpec point,
        int completedCount,
        ExecutionIdentity identity
    )
    {
        string pointRoot = Path.Combine(
            paths.Attempts, attemptId, "point-" + point.Index.ToString("D4")
        );
        Directory.CreateDirectory(pointRoot);
        string className = "ComsolMcpCapacitorPoint" + point.Index.ToString("D4");
        string source = GenerateDriverSource(className, point);
        string javaPath = Path.Combine(pointRoot, className + ".java");
        DurableWriteBytes(javaPath, new UTF8Encoding(false).GetBytes(source));
        string classPath = Path.Combine(pointRoot, className + ".class");
        string compileLog = Path.Combine(paths.Logs, "compile.log");
        string currentPointLog = Path.Combine(paths.Logs, "current-point.log");
        string comsolBatchLog = Path.Combine(pointRoot, "comsol-batch.log");

        WriteStatus(paths, Status(
            "running", attemptId, completedCount, "compiling", point.Id, null
        ));
        int compileExit = RunOwnedProcess(
            Path.Combine(comsolRoot, "bin", "win64", "comsolcompile.exe"),
            Quote(javaPath),
            pointRoot,
            compileLog,
            CompileTimeoutMilliseconds,
            null
        );
        if (compileExit != 0 || !File.Exists(classPath))
        {
            throw new InvalidOperationException("driver_compile_failed");
        }

        int batchExit = RunOwnedProcess(
            Path.Combine(comsolRoot, "bin", "win64", "comsolbatch.exe"),
            "-inputfile " + Quote(classPath) + " -batchlog " + Quote(comsolBatchLog),
            pointRoot,
            currentPointLog,
            PointTimeoutMilliseconds,
            delegate(Process child)
            {
                WriteStatus(paths, Status(
                    "running",
                    attemptId,
                    completedCount,
                    "solving",
                    point.Id,
                    ChildIdentity(child)
                ));
            }
        );
        if (batchExit != 0)
        {
            throw new InvalidOperationException("comsol_batch_failed");
        }

        Dictionary<string, object> result = ReadUniquePointEvent(currentPointLog, point);
        result["attempt_id"] = attemptId;
        result["driver_java_sha256"] = Sha256(javaPath);
        result["driver_class_sha256"] = Sha256(classPath);
        result["process_log_sha256"] = Sha256(currentPointLog);
        result["comsol_batch_log_sha256"] = Sha256(comsolBatchLog);
        result["launcher_sha256"] = identity.LauncherSha256;
        result["campaign_spec_sha256"] = identity.CampaignSpecSha256;
        result["comsol_version"] = identity.ComsolVersion;
        result["comsol_compile_sha256"] = identity.ComsolCompileSha256;
        result["comsol_batch_sha256"] = identity.ComsolBatchSha256;
        return result;
    }

    private static string GenerateDriverSource(string className, PointSpec point)
    {
        string template = ReadEmbeddedText(TemplateResource);
        template = ReplaceOnce(template, "__CLASS_NAME__", className);
        template = ReplaceOnce(template, "__POINT_ID__", point.Id);
        int voltageTokens = CountOccurrences(template, "__VOLTAGE_LITERAL__");
        if (voltageTokens != 2)
        {
            throw new InvalidDataException("embedded_voltage_token_count_changed");
        }
        return template.Replace("__VOLTAGE_LITERAL__", point.VoltageLiteral);
    }

    private static string ReplaceOnce(string source, string token, string value)
    {
        if (CountOccurrences(source, token) != 1)
        {
            throw new InvalidDataException("embedded_template_token_count_changed");
        }
        return source.Replace(token, value);
    }

    private static int CountOccurrences(string source, string token)
    {
        int count = 0;
        int offset = 0;
        while ((offset = source.IndexOf(token, offset, StringComparison.Ordinal)) >= 0)
        {
            count++;
            offset += token.Length;
        }
        return count;
    }

    private static Dictionary<string, object> ReadUniquePointEvent(
        string processLog,
        PointSpec point
    )
    {
        byte[] payload = ReadBounded(processLog, MaximumLogBytes);
        string text = new UTF8Encoding(false, true).GetString(payload);
        List<string> events = new List<string>();
        foreach (string line in SplitLines(text))
        {
            int marker = line.IndexOf(EventPrefix, StringComparison.Ordinal);
            if (marker >= 0)
            {
                events.Add(line.Substring(marker + EventPrefix.Length).Trim());
            }
        }
        if (events.Count != 1)
        {
            throw new InvalidDataException("expected_one_terminal_driver_event");
        }
        Dictionary<string, object> value = ParseObject(events[0]);
        if (Text(value, "schema_name") != "comsol_mcp.standalone_driver_event"
                || Text(value, "schema_version") != "1.0.0"
                || Text(value, "event") != "point_result"
                || Text(value, "status") != "passed"
                || Text(value, "point_id") != point.Id
                || !Boolean(value, "solver_started"))
        {
            throw new InvalidDataException("driver_event_failed_contract");
        }
        ValidatePointPhysics(value, point);
        return value;
    }

    private static void ValidatePointPhysics(
        Dictionary<string, object> value,
        PointSpec point
    )
    {
        RequireFinite(value, "voltage_v");
        RequireFinite(value, "capacitance_pf");
        RequireFinite(value, "relative_error");
        RequireFinite(value, "energy_j");
        RequireFinite(value, "energy_relative_error");
        if (Math.Abs(Number(value, "voltage_v") - point.Voltage) > 1e-12
                || Number(value, "capacitance_pf") <= 0.0
                || Number(value, "energy_j") <= 0.0
                || Number(value, "relative_error") > 1e-6
                || Number(value, "energy_relative_error") > 1e-6)
        {
            throw new InvalidDataException("driver_event_physical_gate_failed");
        }
    }

    private static List<Dictionary<string, object>> ReadAndValidateResults(
        Paths paths,
        ExecutionIdentity identity
    )
    {
        List<Dictionary<string, object>> records = new List<Dictionary<string, object>>();
        if (!File.Exists(paths.Results))
        {
            return records;
        }
        byte[] payload = ReadBounded(paths.Results, MaximumDocumentBytes);
        if (payload.Length != 0 && payload[payload.Length - 1] != (byte)'\n')
        {
            throw new InvalidDataException("results_journal_has_partial_tail");
        }
        string text = new UTF8Encoding(false, true).GetString(payload);
        foreach (string line in SplitLines(text))
        {
            if (string.IsNullOrWhiteSpace(line))
            {
                continue;
            }
            Dictionary<string, object> value = ParseObject(line);
            int expectedIndex = records.Count;
            if (expectedIndex >= Points.Length
                    || Text(value, "point_id") != Points[expectedIndex].Id
                    || Text(value, "schema_name") != "comsol_mcp.standalone_driver_event"
                    || Text(value, "schema_version") != "1.0.0"
                    || Text(value, "event") != "point_result"
                    || Text(value, "status") != "passed"
                    || Text(value, "launcher_sha256") != identity.LauncherSha256
                    || Text(value, "campaign_spec_sha256") != identity.CampaignSpecSha256
                    || Text(value, "comsol_version") != identity.ComsolVersion
                    || Text(value, "comsol_compile_sha256") != identity.ComsolCompileSha256
                    || Text(value, "comsol_batch_sha256") != identity.ComsolBatchSha256)
            {
                throw new InvalidDataException("results_journal_identity_mismatch");
            }
            ValidatePointPhysics(value, Points[expectedIndex]);
            records.Add(value);
        }
        return records;
    }

    private static Dictionary<string, object> ValidateCampaignPhysics(
        List<Dictionary<string, object>> rows
    )
    {
        if (rows.Count != Points.Length)
        {
            throw new InvalidDataException("campaign_is_incomplete");
        }
        double referenceCapacitance = Number(rows[0], "capacitance_pf");
        double referenceEnergyScale = Number(rows[0], "energy_j")
                / Math.Pow(Number(rows[0], "voltage_v"), 2.0);
        if (referenceCapacitance <= 0.0 || referenceEnergyScale <= 0.0)
        {
            throw new InvalidDataException("campaign_reference_physics_is_not_positive");
        }
        double maximumCapacitanceDelta = 0.0;
        double maximumEnergyScaleDelta = 0.0;
        foreach (Dictionary<string, object> row in rows)
        {
            double capacitanceDelta = Math.Abs(
                Number(row, "capacitance_pf") - referenceCapacitance
            ) / referenceCapacitance;
            double energyScale = Number(row, "energy_j")
                    / Math.Pow(Number(row, "voltage_v"), 2.0);
            double energyScaleDelta = Math.Abs(energyScale - referenceEnergyScale)
                    / referenceEnergyScale;
            maximumCapacitanceDelta = Math.Max(maximumCapacitanceDelta, capacitanceDelta);
            maximumEnergyScaleDelta = Math.Max(maximumEnergyScaleDelta, energyScaleDelta);
        }
        if (maximumCapacitanceDelta > 1e-6 || maximumEnergyScaleDelta > 1e-6)
        {
            throw new InvalidDataException("campaign_cross_point_physical_gate_failed");
        }
        return new Dictionary<string, object>
        {
            {"point_count", rows.Count},
            {"maximum_capacitance_relative_delta", maximumCapacitanceDelta},
            {"maximum_energy_over_voltage_squared_relative_delta", maximumEnergyScaleDelta},
            {"status", "passed"}
        };
    }

    private static int RequestPause()
    {
        Paths paths = new Paths(ExecutableDirectory());
        Dictionary<string, object> status = ReadStatus(paths);
        if (Text(status, "status") != "running" || !OwnerActive(paths))
        {
            throw new InvalidOperationException("pause_requires_active_campaign");
        }
        string attemptId = Text(status, "attempt_id");
        Dictionary<string, object> request = new Dictionary<string, object>
        {
            {"schema_name", "comsol_mcp.standalone_pause_request"},
            {"schema_version", "1.0.0"},
            {"request_id", Guid.NewGuid().ToString("N")},
            {"target_attempt", attemptId},
            {"requested_at_utc", UtcNow()}
        };
        AtomicWriteJson(paths.PauseRequest, request);
        Console.WriteLine(Json.Serialize(request));
        return 0;
    }

    private static bool PauseRequested(Paths paths, string attemptId)
    {
        if (!File.Exists(paths.PauseRequest))
        {
            return false;
        }
        Dictionary<string, object> request = ReadJsonObject(paths.PauseRequest);
        return Text(request, "schema_name") == "comsol_mcp.standalone_pause_request"
                && Text(request, "schema_version") == "1.0.0"
                && Text(request, "target_attempt") == attemptId;
    }

    private static void AcknowledgePause(Paths paths, string attemptId, int completed)
    {
        Dictionary<string, object> request = ReadJsonObject(paths.PauseRequest);
        Dictionary<string, object> acknowledgement = new Dictionary<string, object>
        {
            {"schema_name", "comsol_mcp.standalone_pause_ack"},
            {"schema_version", "1.0.0"},
            {"request_id", Text(request, "request_id")},
            {"target_attempt", attemptId},
            {"acknowledged_at_utc", UtcNow()},
            {"completed", completed},
            {"total", Points.Length}
        };
        AtomicWriteJson(paths.PauseAck, acknowledgement);
        WriteTerminalStatus(
            paths, Status("paused", attemptId, completed, "terminal", null, null)
        );
        Log(paths, "pause_acknowledged", Text(request, "request_id"));
        Console.WriteLine(Json.Serialize(acknowledgement));
    }

    private static bool CanResume(Paths paths, ExecutionIdentity identity)
    {
        if (!File.Exists(paths.Status))
        {
            return File.Exists(paths.Results);
        }
        Dictionary<string, object> status = ReadStatus(paths);
        if (Text(status, "launcher_sha256") != identity.LauncherSha256
                || Text(status, "campaign_spec_sha256") != identity.CampaignSpecSha256
                || Text(status, "comsol_version") != identity.ComsolVersion
                || Text(status, "comsol_compile_sha256") != identity.ComsolCompileSha256
                || Text(status, "comsol_batch_sha256") != identity.ComsolBatchSha256)
        {
            throw new InvalidDataException("campaign_status_identity_mismatch");
        }
        string value = Text(status, "status");
        if (value == "paused" || value == "failed" || value == "interrupted")
        {
            return true;
        }
        // RunCampaign calls this only after acquiring the exclusive owner lock.
        // A projected running state therefore represents an interrupted prior owner.
        return value == "running";
    }

    private static int PrintStatus()
    {
        Paths paths = new Paths(ExecutableDirectory());
        Dictionary<string, object> status = ReadStatus(paths);
        bool ownerActive = OwnerActive(paths);
        status["owner_active"] = ownerActive;
        if (Text(status, "status") == "running" && !ownerActive)
        {
            status["effective_status"] = "interrupted";
        }
        else
        {
            status["effective_status"] = Text(status, "status");
        }
        Console.WriteLine(Json.Serialize(status));
        return 0;
    }

    private static int PrintResults()
    {
        Paths paths = new Paths(ExecutableDirectory());
        byte[] payload = ReadBounded(paths.Results, MaximumDocumentBytes);
        Console.Write(new UTF8Encoding(false, true).GetString(payload));
        return 0;
    }

    private static int PrintTail(string[] args)
    {
        int lines = 100;
        if (args.Length == 2
                && (!int.TryParse(args[1], NumberStyles.None, CultureInfo.InvariantCulture, out lines)
                    || lines < 1 || lines > MaximumTailLines))
        {
            throw new ArgumentException("tail lines must be from 1 through 500");
        }
        if (args.Length > 2)
        {
            throw new ArgumentException(Usage());
        }
        Paths paths = new Paths(ExecutableDirectory());
        byte[] payload = ReadBounded(paths.LauncherLog, MaximumLogBytes);
        string[] all = SplitLines(new UTF8Encoding(false, true).GetString(payload));
        int start = Math.Max(0, all.Length - lines);
        for (int index = start; index < all.Length; index++)
        {
            Console.WriteLine(all[index]);
        }
        return 0;
    }

    private static Dictionary<string, object> Status(
        string status,
        string attemptId,
        int completed,
        string phase,
        string pointId,
        Dictionary<string, object> child
    )
    {
        Dictionary<string, object> value = new Dictionary<string, object>
        {
            {"schema_name", "comsol_mcp.standalone_status"},
            {"schema_version", "1.0.0"},
            {"status", status},
            {"attempt_id", attemptId},
            {"completed", completed},
            {"total", Points.Length},
            {"phase", phase},
            {"updated_at_utc", UtcNow()},
            {"launcher_sha256", Sha256(Assembly.GetExecutingAssembly().Location)},
            {"campaign_spec_sha256", CampaignSpecSha256()}
        };
        if (ActiveIdentity != null)
        {
            value["comsol_version"] = ActiveIdentity.ComsolVersion;
            value["comsol_compile_sha256"] = ActiveIdentity.ComsolCompileSha256;
            value["comsol_batch_sha256"] = ActiveIdentity.ComsolBatchSha256;
        }
        if (pointId != null)
        {
            value["current_point"] = pointId;
        }
        if (child != null)
        {
            value["child"] = child;
        }
        return value;
    }

    private static void WriteStatus(Paths paths, Dictionary<string, object> value)
    {
        AtomicWriteJson(paths.Status, value);
    }

    private static void WriteTerminalStatus(
        Paths paths, Dictionary<string, object> value
    )
    {
        WriteStatus(paths, value);
        Dictionary<string, object> receipt = new Dictionary<string, object>(value);
        receipt["schema_name"] = "comsol_mcp.standalone_terminal";
        receipt["schema_version"] = "1.0.0";
        receipt["status_schema_name"] = "comsol_mcp.standalone_status";
        receipt["status_schema_version"] = "1.0.0";
        AtomicWriteJson(paths.Terminal, receipt);
    }

    private static void RequireAttemptBudget(Paths paths)
    {
        string[] attempts = Directory.GetDirectories(paths.Attempts);
        if (attempts.Length >= MaximumAttempts)
        {
            throw new InvalidOperationException("campaign_attempt_limit_reached");
        }
    }

    private static Dictionary<string, object> ReadStatus(Paths paths)
    {
        if (!File.Exists(paths.Status))
        {
            throw new FileNotFoundException("campaign_status_missing", paths.Status);
        }
        Dictionary<string, object> value = ReadJsonObject(paths.Status);
        if (Text(value, "schema_name") != "comsol_mcp.standalone_status"
                || Text(value, "schema_version") != "1.0.0")
        {
            throw new InvalidDataException("campaign_status_schema_mismatch");
        }
        return value;
    }

    private static FileStream AcquireOwnerLock(Paths paths)
    {
        try
        {
            return new FileStream(
                paths.OwnerLock, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None
            );
        }
        catch (IOException exception)
        {
            throw new InvalidOperationException("campaign_owner_is_active", exception);
        }
    }

    private static bool OwnerActive(Paths paths)
    {
        try
        {
            using (FileStream stream = new FileStream(
                paths.OwnerLock, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None
            ))
            {
                return false;
            }
        }
        catch (IOException)
        {
            return true;
        }
    }

    private static void WriteOwner(Paths paths, string attemptId, string comsolRoot)
    {
        Process current = Process.GetCurrentProcess();
        Dictionary<string, object> owner = new Dictionary<string, object>
        {
            {"schema_name", "comsol_mcp.standalone_owner"},
            {"schema_version", "1.0.0"},
            {"attempt_id", attemptId},
            {"pid", current.Id},
            {"creation_time_utc", current.StartTime.ToUniversalTime().ToString("O")},
            {"command_sha256", Sha256Text(string.Join("\0", Environment.GetCommandLineArgs()))},
            {"executable_sha256", Sha256(Assembly.GetExecutingAssembly().Location)},
            {"comsol_root_sha256", Sha256Text(comsolRoot.ToUpperInvariant())},
            {"comsol_version", ActiveIdentity.ComsolVersion},
            {"comsol_compile_sha256", ActiveIdentity.ComsolCompileSha256},
            {"comsol_batch_sha256", ActiveIdentity.ComsolBatchSha256}
        };
        AtomicWriteJson(paths.Owner, owner);
    }

    private static Dictionary<string, object> ChildIdentity(Process process)
    {
        return new Dictionary<string, object>
        {
            {"pid", process.Id},
            {"creation_time_utc", process.StartTime.ToUniversalTime().ToString("O")},
            {"name", process.ProcessName}
        };
    }

    private static int RunOwnedProcess(
        string executable,
        string arguments,
        string workingDirectory,
        string processLog,
        int timeoutMilliseconds,
        Action<Process> started
    )
    {
        ProcessStartInfo startInfo = new ProcessStartInfo();
        startInfo.FileName = executable;
        startInfo.Arguments = arguments;
        startInfo.WorkingDirectory = workingDirectory;
        startInfo.UseShellExecute = false;
        startInfo.CreateNoWindow = true;
        startInfo.RedirectStandardOutput = true;
        startInfo.RedirectStandardError = true;
        startInfo.StandardOutputEncoding = new UTF8Encoding(false, true);
        startInfo.StandardErrorEncoding = new UTF8Encoding(false, true);

        using (Process process = new Process())
        {
            process.StartInfo = startInfo;
            process.Start();
            if (started != null)
            {
                started(process);
            }
            Task<string> stdout = process.StandardOutput.ReadToEndAsync();
            Task<string> stderr = process.StandardError.ReadToEndAsync();
            bool timedOut = !process.WaitForExit(timeoutMilliseconds);
            if (timedOut)
            {
                process.Kill();
                process.WaitForExit();
            }
            else
            {
                process.WaitForExit();
            }
            Exception drainFailure = null;
            bool streamsDrained = false;
            try
            {
                streamsDrained = Task.WaitAll(new Task[] {stdout, stderr}, 5000);
            }
            catch (AggregateException exception)
            {
                drainFailure = exception.Flatten();
            }
            if (timedOut)
            {
                throw new TimeoutException("owned_process_timeout", drainFailure);
            }
            if (!streamsDrained)
            {
                throw new InvalidOperationException(
                    "owned_process_streams_not_drained", drainFailure
                );
            }
            string combined = stdout.Result + stderr.Result;
            byte[] payload = new UTF8Encoding(false).GetBytes(combined);
            if (payload.Length > MaximumLogBytes)
            {
                throw new InvalidDataException("owned_process_log_exceeds_bound");
            }
            DurableAtomicWrite(processLog, payload);
            return process.ExitCode;
        }
    }

    private static void RejectExternalComsolOwners()
    {
        string[] names = new string[]
        {
            "comsol", "comsolbatch", "comsolcompile", "comsolmphserver",
            "comsolclusterbatch", "comsolcluster"
        };
        foreach (string name in names)
        {
            Process[] processes = Process.GetProcessesByName(name);
            try
            {
                if (processes.Length != 0)
                {
                    throw new InvalidOperationException("external_comsol_owner_detected");
                }
            }
            finally
            {
                foreach (Process process in processes)
                {
                    process.Dispose();
                }
            }
        }
    }

    private static void EnterCampaignJob()
    {
        CampaignJob = CreateJobObject(IntPtr.Zero, null);
        if (CampaignJob == IntPtr.Zero)
        {
            throw new InvalidOperationException("windows_job_creation_failed");
        }
        JobObjectExtendedLimitInformation information = new JobObjectExtendedLimitInformation();
        information.BasicLimitInformation.LimitFlags = 0x00002000;
        int length = Marshal.SizeOf(typeof(JobObjectExtendedLimitInformation));
        IntPtr buffer = Marshal.AllocHGlobal(length);
        try
        {
            Marshal.StructureToPtr(information, buffer, false);
            if (!SetInformationJobObject(CampaignJob, 9, buffer, (uint)length))
            {
                throw new InvalidOperationException("windows_job_limit_failed");
            }
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
        if (!AssignProcessToJobObject(CampaignJob, Process.GetCurrentProcess().Handle))
        {
            throw new InvalidOperationException("windows_job_assignment_failed");
        }
    }

    private static void ValidateHost(string comsolRoot)
    {
        if (!Environment.Is64BitOperatingSystem || !Environment.Is64BitProcess)
        {
            throw new PlatformNotSupportedException("windows_x64_required");
        }
        RtlOsVersionInfo os = GetRealOsVersion();
        if (os.MajorVersion != 10 || os.BuildNumber < 10240 || os.ProductType != 1)
        {
            throw new PlatformNotSupportedException("windows_10_or_11_required");
        }
        string compiler = Path.Combine(comsolRoot, "bin", "win64", "comsolcompile.exe");
        string batch = Path.Combine(comsolRoot, "bin", "win64", "comsolbatch.exe");
        string java = Path.Combine(comsolRoot, "java", "win64", "jre", "bin", "java.exe");
        foreach (string path in new string[] {compiler, batch, java})
        {
            if (!File.Exists(path))
            {
                throw new FileNotFoundException("comsol_6_4_runtime_incomplete", path);
            }
        }
        string compilerVersion = NormalizeVersion(
            FileVersionInfo.GetVersionInfo(compiler).FileVersion
        );
        string batchVersion = NormalizeVersion(FileVersionInfo.GetVersionInfo(batch).FileVersion);
        if (!compilerVersion.StartsWith("6.4.", StringComparison.Ordinal)
                || !batchVersion.StartsWith("6.4.", StringComparison.Ordinal))
        {
            throw new PlatformNotSupportedException("comsol_6_4_required");
        }
    }

    private static string ParseComsolPath(string[] args, int offset)
    {
        if (args.Length != offset + 2
                || !string.Equals(args[offset], "--comsol-path", StringComparison.Ordinal))
        {
            throw new ArgumentException(Usage());
        }
        string root = Path.GetFullPath(args[offset + 1]);
        if (!Directory.Exists(root) || !IsAscii(root) || IsLink(root))
        {
            throw new ArgumentException("comsol_path_must_be_existing_ascii_regular_directory");
        }
        return root.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
    }

    private static bool IsLink(string path)
    {
        return (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0;
    }

    private static string ExecutableDirectory()
    {
        string value = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        if (string.IsNullOrWhiteSpace(value) || !IsAscii(value))
        {
            throw new InvalidOperationException("deployment_directory_must_be_ascii");
        }
        return value;
    }

    private static string ReadEmbeddedText(string name)
    {
        using (Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
        {
            if (stream == null || stream.Length < 1 || stream.Length > MaximumDocumentBytes)
            {
                throw new InvalidDataException("embedded_driver_template_missing_or_oversized");
            }
            using (StreamReader reader = new StreamReader(
                stream, new UTF8Encoding(false, true), true
            ))
            {
                return reader.ReadToEnd();
            }
        }
    }

    private static Dictionary<string, object> ReadJsonObject(string path)
    {
        byte[] payload = ReadBounded(path, MaximumDocumentBytes);
        return ParseObject(new UTF8Encoding(false, true).GetString(payload));
    }

    private static Dictionary<string, object> ParseObject(string value)
    {
        Dictionary<string, object> parsed = Json.Deserialize<Dictionary<string, object>>(value);
        if (parsed == null)
        {
            throw new InvalidDataException("JSON document must be an object");
        }
        return parsed;
    }

    private static byte[] ReadBounded(string path, int maximumBytes)
    {
        using (FileStream stream = new FileStream(
            path,
            FileMode.Open,
            FileAccess.Read,
            FileShare.ReadWrite | FileShare.Delete
        ))
        {
            long length = stream.Length;
            if (length < 0 || length > maximumBytes)
            {
                throw new InvalidDataException("bounded file is absent or oversized");
            }
            byte[] payload = new byte[(int)length];
            int offset = 0;
            while (offset < payload.Length)
            {
                int read = stream.Read(payload, offset, payload.Length - offset);
                if (read == 0)
                {
                    throw new InvalidDataException("bounded file changed during read");
                }
                offset += read;
            }
            if (stream.ReadByte() != -1)
            {
                throw new InvalidDataException("bounded file grew during read");
            }
            return payload;
        }
    }

    private static void AtomicWriteJson(string path, Dictionary<string, object> value)
    {
        DurableAtomicWrite(path, new UTF8Encoding(false).GetBytes(Json.Serialize(value) + "\n"));
    }

    private static void DurableAtomicWrite(string path, byte[] payload)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path));
        string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        DurableWriteBytes(temporary, payload);
        if (File.Exists(path))
        {
            File.Replace(temporary, path, null);
        }
        else
        {
            File.Move(temporary, path);
        }
    }

    private static void DurableWriteBytes(string path, byte[] payload)
    {
        using (FileStream stream = new FileStream(
            path, FileMode.CreateNew, FileAccess.Write, FileShare.Read
        ))
        {
            stream.Write(payload, 0, payload.Length);
            stream.Flush(true);
        }
    }

    private static void AppendResult(string path, Dictionary<string, object> value)
    {
        byte[] payload = new UTF8Encoding(false).GetBytes(Json.Serialize(value) + "\n");
        using (FileStream stream = new FileStream(
            path, FileMode.Append, FileAccess.Write, FileShare.Read
        ))
        {
            stream.Write(payload, 0, payload.Length);
            stream.Flush(true);
        }
    }

    private static void Log(Paths paths, string eventName, string detail)
    {
        string line = UtcNow() + " " + eventName + " " + detail + "\n";
        byte[] payload = new UTF8Encoding(false).GetBytes(line);
        using (FileStream stream = new FileStream(
            paths.LauncherLog, FileMode.Append, FileAccess.Write, FileShare.ReadWrite
        ))
        {
            stream.Write(payload, 0, payload.Length);
            stream.Flush(true);
        }
    }

    private static void TryWriteFailureStatus(string reason, int completed)
    {
        try
        {
            Paths paths = new Paths(ExecutableDirectory());
            paths.Create();
            Dictionary<string, object> value = new Dictionary<string, object>
            {
                {"schema_name", "comsol_mcp.standalone_status"},
                {"schema_version", "1.0.0"},
                {"status", "failed"},
                {"completed", completed},
                {"total", Points.Length},
                {"phase", "terminal"},
                {"reason_code", BoundedReason(reason)},
                {"updated_at_utc", UtcNow()},
                {"launcher_sha256", Sha256(Assembly.GetExecutingAssembly().Location)},
                {"campaign_spec_sha256", CampaignSpecSha256()},
                {"journal_completion_authority", true}
            };
            if (ActiveIdentity != null)
            {
                value["comsol_version"] = ActiveIdentity.ComsolVersion;
                value["comsol_compile_sha256"] = ActiveIdentity.ComsolCompileSha256;
                value["comsol_batch_sha256"] = ActiveIdentity.ComsolBatchSha256;
            }
            WriteTerminalStatus(paths, value);
            Log(paths, "campaign_failed", BoundedReason(reason));
        }
        catch (Exception statusException)
        {
            Console.Error.WriteLine(
                "COMSOL MCP standalone failure status could not be written: "
                + statusException
            );
        }
    }

    private static string BoundedReason(string value)
    {
        string text = value ?? "unknown";
        if (text.Length > 128)
        {
            text = text.Substring(0, 128);
        }
        StringBuilder safe = new StringBuilder(text.Length);
        foreach (char character in text)
        {
            safe.Append(char.IsLetterOrDigit(character) || character == '_' ? character : '_');
        }
        return safe.ToString();
    }

    private static string Text(Dictionary<string, object> value, string name)
    {
        object found;
        if (!value.TryGetValue(name, out found) || !(found is string))
        {
            throw new InvalidDataException("missing string field: " + name);
        }
        return (string)found;
    }

    private static bool Boolean(Dictionary<string, object> value, string name)
    {
        object found;
        if (!value.TryGetValue(name, out found) || !(found is bool))
        {
            throw new InvalidDataException("missing boolean field: " + name);
        }
        return (bool)found;
    }

    private static double Number(Dictionary<string, object> value, string name)
    {
        object found;
        if (!value.TryGetValue(name, out found))
        {
            throw new InvalidDataException("missing numeric field: " + name);
        }
        double number = Convert.ToDouble(found, CultureInfo.InvariantCulture);
        if (double.IsNaN(number) || double.IsInfinity(number))
        {
            throw new InvalidDataException("non-finite numeric field: " + name);
        }
        return number;
    }

    private static void RequireFinite(Dictionary<string, object> value, string name)
    {
        Number(value, name);
    }

    private static string Sha256(string path)
    {
        using (SHA256 algorithm = SHA256.Create())
        using (FileStream stream = File.OpenRead(path))
        {
            byte[] hash = algorithm.ComputeHash(stream);
            StringBuilder builder = new StringBuilder(hash.Length * 2);
            foreach (byte item in hash)
            {
                builder.Append(item.ToString("x2", CultureInfo.InvariantCulture));
            }
            return builder.ToString();
        }
    }

    private static string Sha256Text(string value)
    {
        using (SHA256 algorithm = SHA256.Create())
        {
            byte[] hash = algorithm.ComputeHash(new UTF8Encoding(false).GetBytes(value));
            return string.Concat(hash.Select(item => item.ToString("x2")));
        }
    }

    private static string CampaignSpecSha256()
    {
        return Sha256Text(
            "schema=comsol_mcp.standalone_capacitor_campaign;version=1.0.0;"
            + "windows=10,11;x64=true;comsol=6.4;"
            + "plate_side_m=0.01;plate_gap_m=0.001;epsr=2.1;"
            + "voltages_v=1.0,2.0,3.0;point_order=voltage_1V,voltage_2V,voltage_3V"
        );
    }

    private static string[] SplitLines(string value)
    {
        return value.Replace("\r\n", "\n").Replace('\r', '\n').Split('\n');
    }

    private static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    private static string NormalizeVersion(string value)
    {
        return (value ?? string.Empty).Replace(',', '.').Replace(" ", string.Empty);
    }

    private static string UtcNow()
    {
        return DateTime.UtcNow.ToString("O", CultureInfo.InvariantCulture);
    }

    private static bool IsAscii(string value)
    {
        return value.All(character => character <= 127);
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct RtlOsVersionInfo
    {
        public int Size;
        public int MajorVersion;
        public int MinorVersion;
        public int BuildNumber;
        public int PlatformId;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
        public string ServicePack;
        public short ServicePackMajor;
        public short ServicePackMinor;
        public short SuiteMask;
        public byte ProductType;
        public byte Reserved;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectExtendedLimitInformation
    {
        public BasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("ntdll.dll", CharSet = CharSet.Unicode)]
    private static extern int RtlGetVersion(ref RtlOsVersionInfo versionInfo);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

    [DllImport("kernel32.dll")]
    private static extern bool SetInformationJobObject(
        IntPtr job, int informationClass, IntPtr information, uint length
    );

    [DllImport("kernel32.dll")]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    private static RtlOsVersionInfo GetRealOsVersion()
    {
        RtlOsVersionInfo version = new RtlOsVersionInfo();
        version.Size = Marshal.SizeOf(typeof(RtlOsVersionInfo));
        if (RtlGetVersion(ref version) != 0)
        {
            throw new InvalidOperationException("windows_version_probe_failed");
        }
        return version;
    }

    private sealed class PointSpec
    {
        public PointSpec(int index, string id, string voltageLiteral)
        {
            Index = index;
            Id = id;
            VoltageLiteral = voltageLiteral;
            Voltage = double.Parse(voltageLiteral, CultureInfo.InvariantCulture);
        }

        public int Index { get; private set; }
        public string Id { get; private set; }
        public string VoltageLiteral { get; private set; }
        public double Voltage { get; private set; }
    }

    private sealed class ExecutionIdentity
    {
        private ExecutionIdentity()
        {
        }

        public string LauncherSha256 { get; private set; }
        public string CampaignSpecSha256 { get; private set; }
        public string ComsolVersion { get; private set; }
        public string ComsolCompileSha256 { get; private set; }
        public string ComsolBatchSha256 { get; private set; }

        public static ExecutionIdentity Create(string comsolRoot)
        {
            string compiler = Path.Combine(
                comsolRoot, "bin", "win64", "comsolcompile.exe"
            );
            string batch = Path.Combine(comsolRoot, "bin", "win64", "comsolbatch.exe");
            return new ExecutionIdentity
            {
                LauncherSha256 = Sha256(Assembly.GetExecutingAssembly().Location),
                CampaignSpecSha256 = ComsolMcpStandaloneLauncher.CampaignSpecSha256(),
                ComsolVersion = NormalizeVersion(
                    FileVersionInfo.GetVersionInfo(batch).FileVersion
                ),
                ComsolCompileSha256 = Sha256(compiler),
                ComsolBatchSha256 = Sha256(batch)
            };
        }
    }

    private sealed class Paths
    {
        public Paths(string root)
        {
            Root = root;
            Assets = Path.Combine(root, "assets");
            State = Path.Combine(Assets, "state");
            Data = Path.Combine(Assets, "data");
            Logs = Path.Combine(Assets, "logs");
            Control = Path.Combine(Assets, "control");
            Locks = Path.Combine(Assets, "locks");
            Attempts = Path.Combine(Assets, "attempts");
            Status = Path.Combine(State, "status.json");
            Results = Path.Combine(Data, "results.jsonl");
            LauncherLog = Path.Combine(Logs, "launcher.log");
            OwnerLock = Path.Combine(Locks, "campaign.lock");
            Owner = Path.Combine(Locks, "owner.json");
            PauseRequest = Path.Combine(Control, "pause.json");
            PauseAck = Path.Combine(Control, "pause-ack.json");
            Terminal = Path.Combine(State, "terminal.json");
        }

        public string Root { get; private set; }
        public string Assets { get; private set; }
        public string State { get; private set; }
        public string Data { get; private set; }
        public string Logs { get; private set; }
        public string Control { get; private set; }
        public string Locks { get; private set; }
        public string Attempts { get; private set; }
        public string Status { get; private set; }
        public string Results { get; private set; }
        public string LauncherLog { get; private set; }
        public string OwnerLock { get; private set; }
        public string Owner { get; private set; }
        public string PauseRequest { get; private set; }
        public string PauseAck { get; private set; }
        public string Terminal { get; private set; }

        public void Create()
        {
            foreach (string path in new string[]
                {Assets, State, Data, Logs, Control, Locks, Attempts})
            {
                Directory.CreateDirectory(path);
            }
        }
    }
}
