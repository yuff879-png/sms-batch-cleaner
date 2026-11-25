import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.Objects;

/** Host JVM tests for the actual nested parser; no Android runtime or device. */
public final class LockObservationTest {
    private static int checked;

    private static long device(long major, long minor) {
        return ((major & 0xfffff000L) << 32) | ((major & 0xfffL) << 8)
            | ((minor & 0xffffff00L) << 12) | (minor & 0xffL);
    }

    private static void expect(String name, String input, long major, long minor, long inode,
            String state, Integer pid) {
        SmsBridge.LockObservation actual = SmsBridge.LockObservation.parse(
            new ByteArrayInputStream(input.getBytes(StandardCharsets.US_ASCII)), device(major, minor), inode);
        if (!state.equals(actual.state) || !Objects.equals(pid, actual.holderPid)) {
            throw new AssertionError(name + ": expected " + state + "/" + pid
                + ", got " + actual.state + "/" + actual.holderPid);
        }
        checked++;
    }

    public static void main(String[] ignored) {
        expect("POSIX write", "1: POSIX ADVISORY WRITE 123 08:01:77 0 EOF\n",
            8, 1, 77, "holder_observed", 123);
        expect("POSIX read range", "1: POSIX ADVISORY READ 456 08:01:77 100 199\n",
            8, 1, 77, "holder_observed", 456);
        expect("different major", "1: POSIX ADVISORY WRITE 123 09:01:77 0 EOF\n",
            8, 1, 77, "no_holder_observed", null);
        expect("different minor", "1: POSIX ADVISORY WRITE 123 08:02:77 0 EOF\n",
            8, 1, 77, "no_holder_observed", null);
        expect("different inode", "1: POSIX ADVISORY WRITE 123 08:01:78 0 EOF\n",
            8, 1, 77, "no_holder_observed", null);
        expect("waiter ignored", "1: -> POSIX ADVISORY WRITE 123 08:01:77 0 EOF\n",
            8, 1, 77, "no_holder_observed", null);
        expect("waiter before holder", "1: -> POSIX ADVISORY WRITE 123 08:01:77 0 EOF\n"
                + "2: POSIX ADVISORY WRITE 789 08:01:77 0 EOF\n",
            8, 1, 77, "holder_observed", 789);
        expect("OFD unknown owner", "1: OFDLCK ADVISORY WRITE -1 08:01:77 0 EOF\n",
            8, 1, 77, "holder_observed", null);
        expect("OFD read", "1: OFDLCK ADVISORY READ -1 08:01:77 0 100\n",
            8, 1, 77, "holder_observed", null);
        expect("FLOCK is not record conflict", "1: FLOCK ADVISORY WRITE 321 08:01:77 0 EOF\n",
            8, 1, 77, "no_holder_observed", null);
        expect("active LEASE is not record conflict", "1: LEASE ACTIVE READ 321 08:01:77 0 EOF\n",
            8, 1, 77, "no_holder_observed", null);
        expect("breaking LEASE is not record conflict", "1: LEASE BREAKING WRITE 321 08:01:77 0 EOF\n",
            8, 1, 77, "no_holder_observed", null);
        expect("ignored FLOCK before POSIX", "1: FLOCK ADVISORY WRITE 321 08:01:77 0 EOF\n"
                + "2: POSIX ADVISORY WRITE 654 08:01:77 0 EOF\n",
            8, 1, 77, "holder_observed", 654);
        expect("empty visible snapshot", "", 8, 1, 77, "no_holder_observed", null);
        expect("unknown format", "unrecognized output", 8, 1, 77, "unknown", null);
        expect("malformed after holder", "1: POSIX ADVISORY WRITE 123 08:01:77 0 EOF\n"
                + "unrecognized output\n", 8, 1, 77, "unknown", null);
        expect("reversed range", "1: POSIX ADVISORY WRITE 123 08:01:77 20 10\n",
            8, 1, 77, "unknown", null);
        expect("64-bit device and unsigned inode",
            "1: POSIX ADVISORY WRITE 123 abcd1234:fedc5678:18446744073709551615 0 EOF\n",
            0xabcd1234L, 0xfedc5678L, -1, "holder_observed", 123);
        expect("inode overflow", "1: POSIX ADVISORY WRITE 123 08:01:18446744073709551616 0 EOF\n",
            8, 1, 77, "unknown", null);
        String line = "1: POSIX ADVISORY WRITE 123 09:01:77 0 EOF\n";
        StringBuilder many = new StringBuilder();
        for (int i = 0; i < 1024; i++) many.append(line);
        expect("1024 lines accepted", many.toString(), 8, 1, 77, "no_holder_observed", null);
        many.append(line);
        expect("1025 lines rejected", many.toString(), 8, 1, 77, "unknown", null);
        char[] overBytes = new char[65537];
        Arrays.fill(overBytes, ' ');
        expect("64 KiB exceeded", new String(overBytes), 8, 1, 77, "unknown", null);
        expect("NUL rejected", "1: POSIX ADVISORY WRITE 123 08:01:77 0 EOF\u0000",
            8, 1, 77, "unknown", null);
        expect("unlock lease ignored", "1: LEASE BREAKING UNLCK 123 08:01:77 0 EOF\n",
            8, 1, 77, "no_holder_observed", null);
        SmsBridge.LockObservation failed = SmsBridge.LockObservation.parse(new InputStream() {
            @Override public int read() throws IOException {
                throw new IOException("synthetic unreadable proc");
            }
        }, device(8, 1), 77);
        if (!"unknown".equals(failed.state) || failed.holderPid != null) {
            throw new AssertionError("Read failure must remain unknown");
        }
        checked++;
        System.out.println("Lock observation parser tests passed: " + checked);
    }
}
