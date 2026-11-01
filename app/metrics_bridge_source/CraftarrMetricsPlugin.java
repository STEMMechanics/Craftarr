package com.stemmechanics.craftarr.metrics;

import org.bukkit.Bukkit;
import org.bukkit.plugin.java.JavaPlugin;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;

/**
 * A quiet, local bridge for Paper's server performance API.
 *
 * The panel reads metrics.json; no command is sent to the Minecraft console.
 */
public final class CraftarrMetricsPlugin extends JavaPlugin {
    private static final long SAMPLE_PERIOD_TICKS = 100L;
    private final Object writeLock = new Object();
    private volatile long lastWrittenAt;
    private volatile boolean warned;

    @Override
    public void onEnable() {
        getDataFolder().mkdirs();
        Bukkit.getScheduler().runTaskTimer(this, this::sample, 20L, SAMPLE_PERIOD_TICKS);
    }

    private void sample() {
        final long recordedAt = System.currentTimeMillis();
        try {
            double[] tps = Bukkit.getServer().getTPS();
            double currentTps = tps.length > 0 ? tps[0] : Double.NaN;
            double mspt = Bukkit.getServer().getAverageTickTime();
            if (!Double.isFinite(currentTps) || !Double.isFinite(mspt) || currentTps < 0 || mspt < 0) {
                return;
            }
            final String payload = "{\"schema\":1,\"recorded_at\":" + recordedAt
                    + ",\"tps\":" + currentTps
                    + ",\"mspt\":" + mspt
                    + ",\"source\":\"paper-api\"}";
            Bukkit.getScheduler().runTaskAsynchronously(this, () -> writeSnapshot(recordedAt, payload));
        } catch (NoSuchMethodError | RuntimeException error) {
            if (!warned) {
                warned = true;
                getLogger().warning("Paper performance metrics are unavailable; Craftarr TPS/MSPT graphs will remain empty.");
            }
        }
    }

    private void writeSnapshot(long recordedAt, String payload) {
        synchronized (writeLock) {
            if (recordedAt < lastWrittenAt) {
                return;
            }
            Path target = getDataFolder().toPath().resolve("metrics.json");
            Path temporary = getDataFolder().toPath().resolve(".metrics.json." + recordedAt + ".tmp");
            try {
                Files.createDirectories(target.getParent());
                Files.write(
                        temporary,
                        payload.getBytes(StandardCharsets.UTF_8),
                        StandardOpenOption.CREATE,
                        StandardOpenOption.TRUNCATE_EXISTING,
                        StandardOpenOption.WRITE
                );
                try {
                    Files.move(
                            temporary,
                            target,
                            StandardCopyOption.ATOMIC_MOVE,
                            StandardCopyOption.REPLACE_EXISTING
                    );
                } catch (AtomicMoveNotSupportedException error) {
                    Files.move(temporary, target, StandardCopyOption.REPLACE_EXISTING);
                }
                lastWrittenAt = recordedAt;
            } catch (IOException error) {
                if (!warned) {
                    warned = true;
                    getLogger().warning("Unable to write Craftarr metrics snapshot: " + error.getMessage());
                }
                try {
                    Files.deleteIfExists(temporary);
                } catch (IOException ignored) {
                    // Best effort cleanup; the next sample uses a new filename.
                }
            }
        }
    }
}
