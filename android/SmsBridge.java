import android.app.AppOpsManager;
import android.content.ContentResolver;
import android.content.ContentProviderClient;
import android.content.ContentUris;
import android.content.Context;
import android.content.pm.ApplicationInfo;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Binder;
import android.os.IBinder;
import android.os.Looper;
import android.os.Process;
import android.system.Os;
import android.system.OsConstants;
import android.system.StructStat;
import android.telephony.SubscriptionManager;
import android.telephony.SubscriptionInfo;
import android.os.UserHandle;

import org.json.JSONObject;
import org.json.JSONArray;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.FileDescriptor;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.io.OutputStreamWriter;
import java.lang.reflect.Method;
import java.lang.reflect.Constructor;
import java.nio.charset.StandardCharsets;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.channels.OverlappingFileLockException;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.List;

/** Standalone app_process helper. Its stdout is UTF-8 JSONL, never human logs. */
public final class SmsBridge {
    private static final Uri SMS = Uri.parse("content://sms");
    private static final String PACKAGE = "com.android.phone";
    private static final String[] FINGERPRINT_FIELDS = {
        "_id", "date", "address", "body", "type", "thread_id"
    };
    private static final PrintWriter OUT = new PrintWriter(
        new OutputStreamWriter(System.out, StandardCharsets.UTF_8), true);
    private static final int MAX_PLAN_ROWS = 500000;
    private static final long MAX_PLAN_BYTES = 80L * 1024 * 1024;
    private static String stage = "startup";
    private static String contextPackage = "uninitialized";
    private static String opPackage = "uninitialized";
    private static final IBinder EXTERNAL_TOKEN = new Binder();
    private static FileOutputStream lockStream;
    private static FileChannel lockChannel;
    private static FileLock operationLock;
    private ContentResolver resolver;
    private Context context;
    private AppOpsManager appOps;
    private Integer writeMode;
    private Integer readMode;

    public static void main(String[] args) {
        SmsBridge bridge = new SmsBridge();
        try {
            bridge.initialize();
            if (args.length == 1 && "probe".equals(args[0])) {
                bridge.probe();
            } else if (args.length == 1 && "export".equals(args[0])) {
                bridge.export();
            } else if (args.length == 1 && "scan".equals(args[0])) {
                bridge.scan();
            } else if (args.length == 3 && "delete".equals(args[0])) {
                bridge.delete(new File(args[1]), new File(args[2]));
            } else if (args.length == 2 && "diagnose".equals(args[0])) {
                bridge.diagnose(new File(args[1]));
            } else {
                throw new BridgeException("invalid_command", "Invalid helper command.");
            }
            System.exit(0);
        } catch (Throwable failure) {
            // Platform exception messages can contain selections or message contents.
            // Return a stable error code and class, not those messages or a stack trace.
            Throwable cause = failure;
            for (int depth = 0; depth < 8 && cause.getCause() != null; depth++) cause = cause.getCause();
            String code = failure instanceof BridgeException ? ((BridgeException) failure).code
                : cause instanceof SecurityException ? "android_permission_denied" : "android_error";
            String message = failure instanceof BridgeException
                ? failure.getMessage() : "Android operation failed: " + cause.getClass().getSimpleName();
            try {
                String exceptionText = cause.getMessage() == null ? "" : cause.getMessage().toLowerCase(java.util.Locale.ROOT);
                JSONObject classification = new JSONObject();
                for (String hint : new String[] {"permission", "package", "attribution", "uid", "calling", "android", "read_sms", "appops", "find app", "caller", "content provider", "register", "user", "targetsdk", "deviceid", "isolat", "renounced", "access_sms", "proxy", "op_read_sms", "hidden", "system"}) {
                    classification.put(hint, exceptionText.contains(hint));
                }
                JSONArray stack = new JSONArray();
                for (StackTraceElement frame : cause.getStackTrace()) {
                    if (stack.length() >= 12) break;
                    stack.put(frame.getClassName() + "." + frame.getMethodName() + ":" + frame.getLineNumber());
                }
                JSONObject error = new JSONObject().put("event", "error").put("code", code).put("message", message)
                    .put("stage", stage).put("exception_class", cause.getClass().getName())
                    .put("context_package", contextPackage).put("op_package", opPackage)
                    .put("classification", classification).put("stack", stack);
                // Omit modes not yet observed; default 0 would falsely mean allowed.
                if (bridge.readMode != null) error.put("read_sms_mode", bridge.readMode.intValue());
                if (bridge.writeMode != null) error.put("write_sms_mode", bridge.writeMode.intValue());
                emit(error);
            } catch (Throwable ignored) { }
            System.exit(1);
        }
    }

    private void initialize() throws Exception {
        stage = "identity";
        if (Build.VERSION.SDK_INT < 29) {
            throw new BridgeException("unsupported_android", "Android 10 or later is required.");
        }
        if (Process.myUid() != 0) {
            throw new BridgeException("wrong_uid", "Helper must start as root to acquire the SMS provider.");
        }
        acquireOperationLock();
        if (Looper.myLooper() == null) Looper.prepareMainLooper();
        // A standalone app_process has no AMS application registration, so an
        // ordinary resolver cannot acquire providers. Acquire an external handle
        // as root, then permanently drop this execution thread's UID/GID before
        // making any SMS query or mutation. The token's binder death releases the
        // external reference when this short-lived process exits.
        stage = "external_provider";
        Class<?> activityManager = Class.forName("android.app.ActivityManager");
        Method getService = activityManager.getDeclaredMethod("getService");
        getService.setAccessible(true);
        Object manager = getService.invoke(null);
        Class<?> activityManagerInterface = Class.forName("android.app.IActivityManager");
        Method acquireExternal = activityManagerInterface.getMethod("getContentProviderExternal",
            String.class, int.class, IBinder.class, String.class);
        Object holder = acquireExternal.invoke(manager, "sms", 0, EXTERNAL_TOKEN, "sms-cleaner");
        if (holder == null) throw new BridgeException("provider_unavailable", "Android SMS provider is unavailable.");
        Object provider = holder.getClass().getField("provider").get(holder);
        if (provider == null) throw new BridgeException("provider_unavailable", "Android SMS provider is not ready.");
        stage = "drop_identity";
        Os.setgid(1001);
        Os.setuid(1001);
        if (Process.myUid() != 1001) {
            throw new BridgeException("wrong_uid", "Cannot change helper identity to Android phone UID 1001.");
        }
        stage = "activity_thread";
        Class<?> activityThread = Class.forName("android.app.ActivityThread");
        Method systemMain = activityThread.getDeclaredMethod("systemMain");
        systemMain.setAccessible(true);
        Object thread = systemMain.invoke(null);
        Method getContext = activityThread.getDeclaredMethod("getSystemContext");
        getContext.setAccessible(true);
        Context systemContext = (Context) getContext.invoke(thread);
        stage = "package_uid";
        ApplicationInfo app = systemContext.getPackageManager().getApplicationInfo(PACKAGE, 0);
        if (app.uid != Process.myUid()) {
            throw new BridgeException("package_uid_mismatch", "Phone package UID does not match helper UID.");
        }
        stage = "package_context";
        // createPackageContext() inherits the system context's op package and
        // attribution on modern Android. Construct a standalone app context so
        // both the UID and attribution package actually belong to phone.
        Class<?> compatibilityInfo = Class.forName("android.content.res.CompatibilityInfo");
        Object compatibility = compatibilityInfo.getField("DEFAULT_COMPATIBILITY_INFO").get(null);
        Method getPackage = activityThread.getDeclaredMethod("getPackageInfoNoCheck",
            ApplicationInfo.class, compatibilityInfo);
        getPackage.setAccessible(true);
        Object loadedPackage = getPackage.invoke(thread, app, compatibility);
        Class<?> loadedApk = Class.forName("android.app.LoadedApk");
        Class<?> contextImpl = Class.forName("android.app.ContextImpl");
        Method createAppContext = contextImpl.getDeclaredMethod("createAppContext", activityThread, loadedApk);
        createAppContext.setAccessible(true);
        context = (Context) createAppContext.invoke(null, thread, loadedPackage);
        contextPackage = context.getPackageName();
        opPackage = context.getOpPackageName();
        ContentResolver baseResolver = context.getContentResolver();
        stage = "provider_client";
        Class<?> providerInterface = Class.forName("android.content.IContentProvider");
        Constructor<ContentProviderClient> clientConstructor = ContentProviderClient.class
            .getDeclaredConstructor(ContentResolver.class, providerInterface, String.class, boolean.class);
        clientConstructor.setAccessible(true);
        ContentProviderClient client = clientConstructor.newInstance(baseResolver, provider, "sms", true);
        resolver = ContentResolver.wrap(client);
        stage = "appops";
        appOps = (AppOpsManager) context.getSystemService(Context.APP_OPS_SERVICE);
        if (appOps == null) {
            throw new BridgeException("appops_unavailable", "Android SMS access state is unavailable.");
        }
        writeMode = appOps.unsafeCheckOpRawNoThrow("android:write_sms", Process.myUid(), PACKAGE);
        requireReadAccess();
    }

    private static void acquireOperationLock() throws Exception {
        stage = "operation_lock";
        // This parent is root-owned, unlike /data/local/tmp where shell could
        // replace a lock directory. Never follow a pre-existing symlink.
        StructStat parent = Os.lstat("/data/local");
        if (!OsConstants.S_ISDIR(parent.st_mode) || parent.st_uid != 0) {
            throw new BridgeException("unsafe_lock_path", "Root-owned helper lock parent is unavailable.");
        }
        File directory = new File("/data/local/sms-cleaner");
        if (directory.mkdir()) Os.chmod(directory.getPath(), 0700);
        StructStat dir = Os.lstat(directory.getPath());
        if (!OsConstants.S_ISDIR(dir.st_mode) || dir.st_uid != 0 || (dir.st_mode & 0077) != 0) {
            throw new BridgeException("unsafe_lock_path", "Helper lock directory has an unexpected owner or mode.");
        }
        FileDescriptor descriptor = Os.open(new File(directory, "operation.lock").getPath(),
            OsConstants.O_RDWR | OsConstants.O_CREAT | OsConstants.O_CLOEXEC | OsConstants.O_NOFOLLOW, 0600);
        StructStat file = Os.fstat(descriptor);
        if (!OsConstants.S_ISREG(file.st_mode) || file.st_uid != 0
                || file.st_nlink != 1 || (file.st_mode & 0077) != 0) {
            Os.close(descriptor);
            throw new BridgeException("unsafe_lock_path", "Helper lock file has an unexpected owner or mode.");
        }
        lockStream = new FileOutputStream(descriptor);
        lockChannel = lockStream.getChannel();
        try { operationLock = lockChannel.tryLock(); }
        catch (OverlappingFileLockException busy) { operationLock = null; }
        if (operationLock == null) {
            throw new BridgeException("device_busy", "Another SMS cleaner operation is already running on this device.");
        }
    }

    private JSONObject metadata() throws Exception {
        return new JSONObject().put("event", "summary")
            .put("uid", Process.myUid()).put("user_id", 0)
            .put("backend", "external_phone")
            .put("package_name", PACKAGE).put("write_sms_mode", writeMode)
            .put("read_sms_mode", readMode)
            .put("android", Build.VERSION.RELEASE).put("sdk", Build.VERSION.SDK_INT)
            .put("model", Build.MODEL);
    }

    private Cursor query(Uri uri, String[] projection) throws Exception {
        return queryWithAccessCheck(uri, projection, null, null, "_id ASC");
    }

    private void requireReadAccess() throws Exception {
        stage = "read_sms_access";
        readMode = null;
        // The non-raw check maps MODE_FOREGROUND to MODE_ALLOWED. This helper
        // has no foreground app lifecycle, so only raw MODE_ALLOWED is valid.
        readMode = appOps.unsafeCheckOpRawNoThrow("android:read_sms", Process.myUid(), PACKAGE);
        if (readMode.intValue() != AppOpsManager.MODE_ALLOWED) {
            throw new BridgeException("read_not_allowed", "Android SMS read access is not allowed; message count is unknown.");
        }
    }

    private void requireDeleteAccess() throws Exception {
        requireReadAccess();
        stage = "write_sms_access";
        writeMode = null;
        writeMode = appOps.unsafeCheckOpRawNoThrow("android:write_sms", Process.myUid(), PACKAGE);
        if (writeMode.intValue() != AppOpsManager.MODE_ALLOWED) {
            throw new BridgeException("write_not_allowed", "Android SMS write access is not allowed for this helper.");
        }
    }

    private Cursor queryWithAccessCheck(Uri uri, String[] projection, String selection,
            String[] selectionArgs, String sortOrder) throws Exception {
        // A denied READ_SMS AppOp can produce an empty cursor rather than an
        // exception. Check on both sides so it cannot be treated as a zero count
        // or a missing planned record. Every destructive per-ID query uses this.
        // These checks reject observed revocation; they are not atomic with a
        // provider operation or a rapid revoke-and-restore between checks.
        requireReadAccess();
        stage = "query";
        Cursor cursor = resolver.query(uri, projection, selection, selectionArgs, sortOrder);
        try {
            requireReadAccess();
            if (cursor == null) {
                throw new BridgeException("null_cursor", "SMS provider returned no query result.");
            }
            return cursor;
        } catch (Exception failure) {
            if (cursor != null) cursor.close();
            throw failure;
        }
    }

    private void probe() throws Exception {
        long count = 0;
        long lastHeartbeat = System.nanoTime();
        JSONObject types = new JSONObject();
        try (Cursor cursor = query(SMS, new String[] {"_id", "type"})) {
            while (cursor.moveToNext()) {
                increment(types, cursor.isNull(1) ? "unknown" : cursor.getString(1));
                count++;
                if (System.nanoTime() - lastHeartbeat >= 1000000000L) {
                    status("scanning", count);
                    lastHeartbeat = System.nanoTime();
                }
            }
        }
        requireReadAccess();
        emit(metadata().put("count", count).put("types", types));
    }

    private void export() throws Exception {
        long count = 0;
        JSONObject types = new JSONObject();
        try (Cursor cursor = query(SMS, null)) {
            for (String field : FINGERPRINT_FIELDS) cursor.getColumnIndexOrThrow(field);
            while (cursor.moveToNext()) {
                JSONObject record = record(cursor);
                emit(new JSONObject().put("event", "sms").put("record", record)
                    .put("fingerprint", fingerprint(record)));
                increment(types, record.isNull("type") ? "unknown" : record.getString("type"));
                count++;
            }
        }
        requireReadAccess();
        emit(metadata().put("count", count).put("types", types));
    }

    /** Build an ID/fingerprint plan without exporting message contents to the PC. */
    private void scan() throws Exception {
        long count = 0;
        JSONObject types = new JSONObject();
        try (Cursor cursor = query(SMS, FINGERPRINT_FIELDS)) {
            while (cursor.moveToNext()) {
                JSONObject row = record(cursor);
                emit(new JSONObject().put("event", "target").put("id", row.getString("_id"))
                    .put("fingerprint", fingerprint(row)));
                increment(types, row.isNull("type") ? "unknown" : row.getString("type"));
                count++;
            }
        }
        requireReadAccess();
        emit(metadata().put("count", count).put("types", types));
    }

    private static JSONObject record(Cursor cursor) throws Exception {
        JSONObject row = new JSONObject();
        for (int i = 0; i < cursor.getColumnCount(); i++) {
            if (cursor.getType(i) == Cursor.FIELD_TYPE_BLOB) {
                throw new BridgeException("unsupported_column", "SMS provider contains an unsupported binary column.");
            }
            row.put(cursor.getColumnName(i), cursor.isNull(i) ? JSONObject.NULL : cursor.getString(i));
        }
        return row;
    }

    private static String fingerprint(JSONObject record) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        for (String field : FINGERPRINT_FIELDS) {
            if (!record.has(field)) throw new BridgeException("missing_column", "SMS provider is missing a required column.");
            if (record.isNull(field)) {
                digest.update("-1:".getBytes(StandardCharsets.US_ASCII));
            } else {
                byte[] value = record.getString(field).getBytes(StandardCharsets.UTF_8);
                digest.update((value.length + ":").getBytes(StandardCharsets.US_ASCII));
                digest.update(value);
            }
        }
        StringBuilder hex = new StringBuilder(64);
        for (byte value : digest.digest()) hex.append(String.format(java.util.Locale.ROOT, "%02x", value & 255));
        return hex.toString();
    }

    private static LinkedHashMap<Long, String> loadPlan(File file) throws Exception {
        if (!file.isFile() || file.length() > MAX_PLAN_BYTES) {
            throw new BridgeException("invalid_plan", "Deletion plan is missing or too large.");
        }
        LinkedHashMap<Long, String> plan = new LinkedHashMap<>();
        long lastHeartbeat = System.nanoTime();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(
                new FileInputStream(file), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                if (line.length() > 512 || line.length() == 0 || plan.size() >= MAX_PLAN_ROWS) {
                    throw new BridgeException("invalid_plan", "Deletion plan exceeds validated limits.");
                }
                JSONObject entry = new JSONObject(line);
                if (entry.length() != 2 || !(entry.opt("id") instanceof String)
                        || !(entry.opt("fingerprint") instanceof String)) {
                    throw new BridgeException("invalid_plan", "Deletion plan has an invalid record.");
                }
                String idText = entry.getString("id");
                String hash = entry.getString("fingerprint");
                if (!idText.matches("[1-9][0-9]{0,18}") || !hash.matches("[0-9a-f]{64}")) {
                    throw new BridgeException("invalid_plan", "Deletion plan has an invalid identifier or fingerprint.");
                }
                long id;
                try { id = Long.parseLong(idText); }
                catch (NumberFormatException badId) {
                    throw new BridgeException("invalid_plan", "Deletion plan identifier is out of range.");
                }
                if (plan.put(id, hash) != null) {
                    throw new BridgeException("invalid_plan", "Deletion plan contains duplicate identifiers.");
                }
                if (System.nanoTime() - lastHeartbeat >= 1000000000L) {
                    status("validating_plan", plan.size());
                    lastHeartbeat = System.nanoTime();
                }
            }
        }
        return plan;
    }

    /** Read-only diagnostics; never returns message bodies or sender addresses. */
    private void diagnose(File planFile) throws Exception {
        LinkedHashMap<Long, String> plan = loadPlan(planFile);
        if (plan.size() != 1) {
            throw new BridgeException("invalid_plan", "Read-only diagnostics require exactly one planned message.");
        }
        JSONArray diagnostics = new JSONArray();
        for (Map.Entry<Long, String> item : plan.entrySet()) {
            JSONObject result = new JSONObject().put("id", Long.toString(item.getKey()));
            JSONObject row = null;
            try (Cursor cursor = query(ContentUris.withAppendedId(SMS, item.getKey()), null)) {
                if (cursor.moveToFirst()) row = record(cursor);
            }
            result.put("exists", row != null);
            if (row != null) {
                result.put("fingerprint_matches", item.getValue().equals(fingerprint(row)));
                JSONObject flags = new JSONObject();
                for (String field : new String[] {"locked", "deleted", "block_type", "sync_state",
                        "sim_id", "sub_id", "type", "read", "seen", "thread_id"}) {
                    if (row.has(field)) {
                        Object value = row.isNull(field) ? JSONObject.NULL : row.getString(field);
                        if (value != JSONObject.NULL && !value.toString().matches("-?[0-9]+")) value = "present_non_numeric";
                        flags.put(field, value);
                    }
                }
                result.put("flags", flags);
                String threadId = row.isNull("thread_id") ? "" : row.getString("thread_id");
                if (!threadId.matches("[0-9]{1,10}")) {
                    throw new BridgeException("invalid_thread", "SMS provider returned an unsupported conversation identifier.");
                }
                Uri threadUri = Uri.parse("content://sms/conversations/" + threadId);
                StringBuilder where = new StringBuilder();
                ArrayList<String> values = new ArrayList<>();
                JSONArray matching = new JSONArray();
                for (String field : FINGERPRINT_FIELDS) {
                    if (where.length() != 0) where.append(" AND ");
                    where.append(field);
                    if (row.isNull(field)) where.append(" IS NULL");
                    else { where.append(" = ?"); values.add(row.getString(field)); }
                    matching.put(new JSONObject().put("added_field", field)
                        .put("sms_count", countMatching(SMS, where.toString(), values))
                        .put("conversation_count", countMatching(threadUri, where.toString(), values)));
                }
                result.put("predicate_counts", matching);
            }
            diagnostics.put(result);
        }
        JSONObject subscriptions = new JSONObject();
        SubscriptionManager manager = context.getSystemService(SubscriptionManager.class);
        try {
            Method associated = SubscriptionManager.class.getMethod("getSubscriptionInfoListAssociatedWithUser", UserHandle.class);
            Object value = associated.invoke(manager, Process.myUserHandle());
            JSONArray ids = new JSONArray();
            if (value instanceof List<?>) {
                for (Object entry : (List<?>) value) ids.put(((SubscriptionInfo) entry).getSubscriptionId());
            }
            subscriptions.put("associated_ids", ids);
        } catch (Throwable unsupported) {
            subscriptions.put("associated_ids_unavailable", unsupported.getClass().getSimpleName());
        }
        try {
            JSONArray ids = new JSONArray();
            List<SubscriptionInfo> active = manager.getActiveSubscriptionInfoList();
            if (active != null) for (SubscriptionInfo entry : active) ids.put(entry.getSubscriptionId());
            subscriptions.put("active_ids", ids);
        } catch (Throwable unsupported) {
            subscriptions.put("active_ids_unavailable", unsupported.getClass().getSimpleName());
        }
        requireReadAccess();
        emit(metadata().put("diagnostics", diagnostics).put("subscriptions", subscriptions)
            .put("read_only", true));
    }

    private long countMatching(Uri uri, String where, ArrayList<String> values) throws Exception {
        try (Cursor cursor = queryWithAccessCheck(uri, new String[] {"_id"}, where,
                values.toArray(new String[0]), null)) {
            long count = cursor.getCount();
            requireReadAccess();
            return count;
        }
    }

    private void delete(File planFile, File cancelFile) throws Exception {
        requireDeleteAccess();
        LinkedHashMap<Long, String> plan = loadPlan(planFile);
        long deleted = 0, changed = 0, missing = 0, failed = 0, processed = 0;
        long lastProgress = System.nanoTime();
        boolean cancelled = false;
        emit(progress("progress", plan.size(), processed, deleted, changed, missing, failed));
        for (Map.Entry<Long, String> item : plan.entrySet()) {
            if (cancelFile.exists()) { cancelled = true; break; }
            Uri uri = ContentUris.withAppendedId(SMS, item.getKey());
            JSONObject row = null;
            try (Cursor cursor = query(uri, FINGERPRINT_FIELDS)) {
                if (cursor.moveToFirst()) row = record(cursor);
            }
            requireReadAccess();
            if (row == null) {
                missing++;
            } else if (!item.getValue().equals(fingerprint(row))) {
                changed++;
            } else {
                if (cancelFile.exists()) { cancelled = true; break; }
                // SMS_ALL_ID refreshes the owning conversation and does not apply
                // the newer conversation endpoint's subscription filtering. AOSP
                // ignores selection on this URI, so do not pretend a SQL predicate
                // makes this atomic: a message can change between the fingerprint
                // query and delete. Scope remains exactly the planned numeric ID.
                requireDeleteAccess();
                stage = "delete";
                int affected = resolver.delete(uri, null, null);
                if (affected == 1) deleted++;
                else {
                    failed++;
                    processed++;
                    emit(progress("progress", plan.size(), processed, deleted, changed, missing, failed));
                    // A denied or unexpected provider result stops the run, rather than
                    // repeatedly attempting destructive operations with unclear effects.
                    break;
                }
            }
            processed++;
            if (processed % 50 == 0 || processed == plan.size()
                    || System.nanoTime() - lastProgress >= 1000000000L) {
                emit(progress("progress", plan.size(), processed, deleted, changed, missing, failed));
                lastProgress = System.nanoTime();
            }
        }
        long remainingTarget = 0, remainingTotal = 0;
        Set<Long> targetIds = plan.keySet();
        try (Cursor cursor = query(SMS, new String[] {"_id"})) {
            while (cursor.moveToNext()) {
                remainingTotal++;
                if (targetIds.contains(cursor.getLong(0))) remainingTarget++;
                if (System.nanoTime() - lastProgress >= 1000000000L) {
                    status("verifying", remainingTotal);
                    lastProgress = System.nanoTime();
                }
            }
        }
        requireReadAccess();
        emit(progress("summary", plan.size(), processed, deleted, changed, missing, failed)
            .put("remaining_target", remainingTarget).put("remaining_total", remainingTotal)
            .put("cancelled", cancelled).put("verified", true));
    }

    private static JSONObject progress(String event, long total, long processed, long deleted,
            long changed, long missing, long failed) throws Exception {
        return new JSONObject().put("event", event).put("total", total)
            .put("processed", processed).put("deleted", deleted).put("changed", changed)
            .put("missing", missing).put("failed", failed);
    }

    private static void increment(JSONObject object, String key) throws Exception {
        object.put(key, object.optLong(key, 0) + 1);
    }

    private static void status(String phase, long processed) throws Exception {
        emit(new JSONObject().put("event", "status").put("phase", phase).put("processed", processed));
    }

    private static void emit(JSONObject value) throws Exception {
        OUT.println(value.toString());
        if (OUT.checkError()) throw new BridgeException("output_closed", "Host connection closed.");
    }

    private static final class BridgeException extends Exception {
        final String code;
        BridgeException(String code, String message) { super(message); this.code = code; }
    }
}
