/*
 * JT9 Decoder Wrapper - Standalone FT2/FT4/FT8 decoder using jt9
 *
 * Provenance: forked from madpsy/jt9-decode @ 1b8507a4 (2026-05-10). That
 * wrapper embeds WSJT-X's commons.h and drives WSJT-X's jt9 over WSJT-X's
 * shared-memory decode protocol, so it is a derivative work of WSJT-X and is
 * therefore licensed under the GNU General Public License v3 (see PROVENANCE.md;
 * WSJT-X COPYING is retained by the wsjtx-decoders build). No warranty.
 *
 * Sigmond modifications (2026-07-22), see PROVENANCE.md — build-behaviour, not
 * decoder logic:
 *   1. Added TRIGGERED mode (-T/--triggered): decode each fed slot in stream
 *      order, immediately, with NO wall-clock timing. The caller (psk-recorder)
 *      writes exactly one GPS-aligned slot per cadence and owns the slot UTC.
 *      The stock -s stream mode self-times on wall-clock, which drifts vs the
 *      RTP/GPS anchor — unusable for a GPS-disciplined site.
 *   2. Fixed a latent ipc handshake race exposed by back-to-back resident
 *      decodes (idle-gate on ipc[1]/ipc[2] before issuing the next slot); the
 *      stock one-shot and 15 s-gap stream paths never exercised it.
 *
 * A command-line wrapper for the WSJT-X jt9 decoder engine that supports:
 * - FT2, FT4, and FT8 digital modes
 * - WAV file decoding
 * - Continuous streaming from stdin (PCM audio), wall-clock (-s) or caller-
 *   triggered (-T)
 * - Mode-specific cycle timing with UTC alignment (stock -s mode only)
 *
 * Uses Qt's QSharedMemory for IPC with jt9, implementing the same
 * shared memory protocol as WSJT-X.
 *
 * ARCHITECTURE: Asynchronous event-driven model matching WSJT-X
 * - Uses Qt signals/slots for jt9 communication
 * - Non-blocking decode processing
 * - Proper state management and acknowledgment
 *
 * Outputs diagnostic messages to stderr, decoded messages to stdout
 */

#include <QCoreApplication>
#include <QSharedMemory>
#include <QProcess>
#include <QFile>
#include <QDataStream>
#include <QThread>
#include <QTextStream>
#include <QMutex>
#include <QWaitCondition>
#include <QDir>
#include <QDateTime>
#include <QTimer>
#include <QObject>
#include <cstring>
#include <ctime>
#include <cmath>
#include <atomic>

extern "C" {
#include "commons.h"
}

// Helper to write to stderr
QTextStream qStdErr(stderr);
QTextStream qStdOut(stdout);

// Read WAV file
int read_wav_file(const QString &filename, short *audio_data, int max_samples) {
    QFile file(filename);
    if (!file.open(QIODevice::ReadOnly)) {
        qStdErr << "Error: Cannot open file " << filename << "\n";
        qStdErr.flush();
        return -1;
    }
    
    QDataStream in(&file);
    in.setByteOrder(QDataStream::LittleEndian);
    
    // Read RIFF header
    char riff[4], wave[4];
    quint32 file_size;
    in.readRawData(riff, 4);
    in >> file_size;
    in.readRawData(wave, 4);
    
    if (memcmp(riff, "RIFF", 4) != 0 || memcmp(wave, "WAVE", 4) != 0) {
        qStdErr << "Error: Not a valid WAV file\n";
        qStdErr.flush();
        return -1;
    }
    
    // Read fmt chunk
    char fmt[4];
    quint32 fmt_size;
    quint16 audio_format, num_channels, bits_per_sample, block_align;
    quint32 sample_rate, byte_rate;
    
    in.readRawData(fmt, 4);
    in >> fmt_size;
    in >> audio_format;
    in >> num_channels;
    in >> sample_rate;
    in >> byte_rate;
    in >> block_align;
    in >> bits_per_sample;
    
    // Skip any extra fmt bytes
    if (fmt_size > 16) {
        file.seek(file.pos() + fmt_size - 16);
    }
    
    // Search for data chunk
    char chunk_id[4];
    quint32 chunk_size;
    quint32 data_size = 0;
    bool found_data = false;
    
    while (!found_data && in.readRawData(chunk_id, 4) == 4) {
        in >> chunk_size;
        
        if (memcmp(chunk_id, "data", 4) == 0) {
            data_size = chunk_size;
            found_data = true;
            qStdErr << "Found data chunk, size: " << data_size << " bytes\n";
        } else {
            qStdErr << "Skipping chunk \"" << QString::fromLatin1(chunk_id, 4) << "\" (" << chunk_size << " bytes)\n";
            file.seek(file.pos() + chunk_size);
        }
    }
    
    if (!found_data) {
        qStdErr << "Could not find data chunk in WAV file\n";
        qStdErr.flush();
        return -1;
    }
    
    qStdErr << "WAV file info:\n";
    qStdErr << "  Sample rate: " << sample_rate << " Hz\n";
    qStdErr << "  Channels: " << num_channels << "\n";
    qStdErr << "  Bits per sample: " << bits_per_sample << "\n";
    qStdErr << "  Data size: " << data_size << " bytes\n";
    
    int samples_to_read = data_size / sizeof(short);
    if (num_channels == 2) {
        samples_to_read /= 2;  // Stereo
    }
    if (samples_to_read > max_samples) {
        samples_to_read = max_samples;
    }
    
    // Read audio data
    if (num_channels == 1) {
        // Mono
        int read = in.readRawData((char*)audio_data, samples_to_read * sizeof(short)) / sizeof(short);
        qStdErr << "  Read " << read << " samples\n";
        qStdErr.flush();
        return read;
    } else {
        // Stereo - take left channel
        short stereo_buf[2];
        int i;
        for (i = 0; i < samples_to_read; i++) {
            if (in.readRawData((char*)stereo_buf, 4) != 4) break;
            audio_data[i] = stereo_buf[0];
        }
        qStdErr << "  Read " << i << " samples (stereo -> mono)\n";
        qStdErr.flush();
        return i;
    }
}

// Mode configuration structure
struct ModeConfig {
    int mode_code;      // jt9 mode code
    int cycle_ms;       // cycle time in milliseconds
    int ihsym;          // number of symbols
    int nzhsym;         // hsymStop - symbols to decode (WSJT-X m_hsymStop)
    const char* name;   // mode name
};

// Mode configurations (matching WSJT-X lines 2207, 2211, 2213)
const ModeConfig MODE_FT2  = {52, 3750, 105, 21, "FT2"};   // 3.75 seconds, hsymStop=21
const ModeConfig MODE_FT4  = {5,  7500, 105, 21, "FT4"};   // 7.5 seconds, hsymStop=21
const ModeConfig MODE_FT8  = {8,  15000, 50, 50, "FT8"};   // 15 seconds, hsymStop=50

// Audio reader thread - continuously reads samples from stdin
class AudioReaderThread : public QThread {
public:
    AudioReaderThread(short *buffer, int buffer_size, QMutex *mutex)
        : circ_buffer(buffer), buffer_size(buffer_size), buffer_mutex(mutex),
          write_pos(0), total_samples(0), should_stop(false) {}
    
    void run() override {
        FILE *stdin_file = stdin;
        #ifdef _WIN32
        _setmode(_fileno(stdin), _O_BINARY);
        #endif
        
        short sample_buf[4096];
        
        while (!should_stop) {
            size_t samples_read = fread(sample_buf, sizeof(short), 4096, stdin_file);

            if (samples_read == 0) {
                if (feof(stdin_file) || ferror(stdin_file)) {
                    eof_reached = true;   // stdin closed — no more slots coming
                    break;
                }
                QThread::msleep(10);
                continue;
            }
            
            // Lock and copy samples to circular buffer
            buffer_mutex->lock();
            for (size_t i = 0; i < samples_read; i++) {
                circ_buffer[write_pos] = sample_buf[i];
                write_pos = (write_pos + 1) % buffer_size;
                total_samples++;
            }
            buffer_mutex->unlock();
        }
    }
    
    void stop() { should_stop = true; }
    qint64 getTotalSamples() { return total_samples.load(); }
    int getWritePos() { return write_pos.load(); }
    bool isEof() { return eof_reached.load(); }

private:
    short *circ_buffer;
    int buffer_size;
    QMutex *buffer_mutex;
    std::atomic<int> write_pos;
    std::atomic<qint64> total_samples;
    std::atomic<bool> should_stop;
    std::atomic<bool> eof_reached{false};
};

// Forward declaration
class StreamDecoder;

// Asynchronous stream decoder - matches WSJT-X architecture
class StreamDecoder : public QObject {
    Q_OBJECT
    
public:
    StreamDecoder(QSharedMemory *shm, dec_data_t *dec, QProcess *jt9_proc,
                  const ModeConfig &mode_cfg, bool triggered = false,
                  QObject *parent = nullptr)
        : QObject(parent), sharedMemory(shm), dec_data(dec), jt9(jt9_proc),
          mode(mode_cfg), triggered_(triggered), consumed_(0),
          decode_in_progress(false), total_decodes(0),
          skipped_cycles(0), jt9_decode_count(0), watchdog_fires(0)
    {
        SAMPLES_PER_CYCLE = (RX_SAMPLE_RATE * mode.cycle_ms) / 1000;
        BUFFER_SIZE = NTMAX * RX_SAMPLE_RATE;
        
        // Allocate circular buffer
        circ_buffer = new short[BUFFER_SIZE];
        
        // Start audio reader thread
        reader_thread = new AudioReaderThread(circ_buffer, BUFFER_SIZE, &buffer_mutex);
        reader_thread->start();
        
        // Connect jt9 output to our handler (WSJT-X style)
        connect(jt9, &QProcess::readyReadStandardOutput, this, &StreamDecoder::readFromStdout);
        
        // Connect jt9 error/finished signals for health monitoring
        connect(jt9, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished),
                this, &StreamDecoder::jt9Finished);
        connect(jt9, &QProcess::errorOccurred, this, &StreamDecoder::jt9Error);
        
        // Timer for cycle boundaries
        cycle_timer = new QTimer(this);
        connect(cycle_timer, &QTimer::timeout, this, &StreamDecoder::onCycleTimer);

        // Watchdog: resets stuck decode_in_progress if jt9 never sends <DecodeFinished>
        decode_watchdog = new QTimer(this);
        decode_watchdog->setSingleShot(true);
        connect(decode_watchdog, &QTimer::timeout, this, &StreamDecoder::onDecodeWatchdog);
        
        qStdErr << (triggered_ ? "Triggered mode: " : "Stream mode: ")
                << "Reading 12kHz 16-bit mono PCM from stdin\n";
        qStdErr << mode.name << " cycle time: " << mode.cycle_ms << " ms (" << SAMPLES_PER_CYCLE << " samples)\n";
        if (triggered_)
            qStdErr << "Decoding each fed slot in order; caller owns timing\n";
        else
            qStdErr << "Triggering decodes at UTC-aligned " << (mode.cycle_ms / 1000.0) << " second boundaries\n";
        qStdErr.flush();
    }
    
    ~StreamDecoder() {
        if (reader_thread) {
            reader_thread->stop();
            reader_thread->wait();
            delete reader_thread;
        }
        delete[] circ_buffer;
    }
    
    void start() {
        if (triggered_) {
            // Caller (psk-recorder) owns slot timing: it writes exactly one
            // GPS-aligned slot's PCM per cadence and stamps the authoritative
            // UTC downstream.  We decode each full slot in stream order,
            // immediately, never skipping — no wall-clock timer.  Poll fast;
            // the reader thread drains stdin so the caller never blocks.
            qStdErr << "Starting triggered decode loop...\n";
            qStdErr.flush();
            cycle_timer->start(TRIGGER_POLL_MS);  // onCycleTimer dispatches
            return;
        }
        qStdErr << "Waiting for first cycle boundary...\n";
        qStdErr.flush();

        // Wait for enough samples to accumulate
        while (reader_thread->getTotalSamples() < SAMPLES_PER_CYCLE) {
            QThread::msleep(100);
            QCoreApplication::processEvents();
        }
        
        // Calculate time to next cycle boundary
        qint64 wait_ms = msToNextCycle();
        if (wait_ms > 100) {
            qStdErr << "Waiting " << wait_ms << " ms for cycle boundary...\n";
            qStdErr.flush();
            QThread::msleep(wait_ms);
        }
        
        qStdErr << "Starting decode loop...\n";
        qStdErr.flush();
        
        // Start cycle timer - triggers at each cycle boundary
        cycle_timer->start(mode.cycle_ms);
        
        // Trigger first decode immediately
        onCycleTimer();
    }
    
private slots:
    // Called when jt9 has output ready (WSJT-X style: readFromStdout)
    void readFromStdout() {
        while (jt9->canReadLine()) {
            QString line = QString::fromLocal8Bit(jt9->readLine()).trimmed();
            
            // Check for decode finished marker (matching WSJT-X line 6233)
            if (line.indexOf("<DecodeFinished>") >= 0) {
                // Extract decode count from <DecodeFinished> line
                // Format: "<DecodeFinished>   nsynced  ndecoded  navg"
                // We want the second number (ndecoded)
                QStringList parts = line.mid(16).trimmed().split(QRegExp("\\s+"), Qt::SkipEmptyParts);
                if (parts.size() >= 2) {
                    jt9_decode_count = parts[1].toInt();
                }
                decodeDone();  // Call decodeDone like WSJT-X does (line 6244)
                return;
            } else if (line.length() > 6 && line[0].isDigit() && !line.startsWith('<')) {
                // Actual decode line - output to stdout
                qStdOut << line << "\n";
                qStdOut.flush();
            } else if (!line.isEmpty()) {
                // Debug/diagnostic output
                qStdErr << "jt9: " << line << "\n";
                qStdErr.flush();
            }
        }
    }
    
    // Poll (triggered mode): decode each fed slot in order, never skipping.
    void onTriggeredTick() {
        if (jt9->state() != QProcess::Running) {
            qStdErr << "Error: jt9 process is not running!\n";
            qStdErr.flush();
            QCoreApplication::quit();
            return;
        }
        // One decode at a time: don't advance while jt9 is busy (no skip).
        if (decode_in_progress) return;

        // Back-to-back decodes race the ipc handshake: jt9a.f90 finishes a
        // decode by waiting for our ack ipc[2]==1, then clears ipc[2]→0 and
        // loops to wait for the next start ipc[1]==1.  In wall-clock stream
        // mode the 15 s gap hides this; here we fire immediately, so we must
        // wait until jt9 has consumed the previous ack and returned to idle
        // (ipc[1]==0 && ipc[2]==0) before issuing the next slot.  Firing early
        // leaves jt9 stuck waiting for an ack it already got → watchdog hang.
        sharedMemory->lock();
        int ipc1 = dec_data->ipc[1];
        int ipc2 = dec_data->ipc[2];
        sharedMemory->unlock();
        if (ipc1 != 0 || ipc2 != 0) return;   // jt9 not idle yet — retry next poll

        qint64 avail = reader_thread->getTotalSamples() - consumed_;
        if (avail < SAMPLES_PER_CYCLE) {
            // No full slot yet.  If stdin has closed and no full slot remains,
            // we've drained everything the caller sent — exit cleanly.
            if (reader_thread->isEof()) {
                qStdErr << "stdin closed; " << avail
                        << " leftover samples (< one slot), exiting\n";
                qStdErr.flush();
                QCoreApplication::quit();
            }
            return;
        }
        triggerDecodeInOrder(consumed_);
        consumed_ += SAMPLES_PER_CYCLE;
    }

    // In-order decode of exactly one slot starting at absolute sample index
    // `start_index` (triggered mode).  Mirrors onCycleTimer's shmem handshake
    // but selects the NEXT slot in stream order rather than "the last N".
    void triggerDecodeInOrder(qint64 start_index) {
        decode_in_progress = true;
        decode_start_ms = getUtcMs();
        decode_watchdog->start(mode.cycle_ms * 2);

        // nutc is a wall-clock label only; the caller stamps the authoritative
        // slot time downstream, so its exact value does not affect decoding.
        time_t now = time(NULL);
        struct tm *tm_info = gmtime(&now);
        int nutc = tm_info->tm_hour * 100 + tm_info->tm_min;

        total_decodes++;
        qStdErr << "Triggered decode #" << total_decodes
                << " (slot samples " << start_index << ".."
                << (start_index + SAMPLES_PER_CYCLE) << ")\n";
        qStdErr.flush();

        // Copy exactly this slot's samples, IN ORDER, to shared memory.
        buffer_mutex.lock();
        int read_start = (int)(start_index % BUFFER_SIZE);
        if (read_start + SAMPLES_PER_CYCLE <= BUFFER_SIZE) {
            memcpy(dec_data->d2, circ_buffer + read_start, SAMPLES_PER_CYCLE * sizeof(short));
        } else {
            int first_part = BUFFER_SIZE - read_start;
            memcpy(dec_data->d2, circ_buffer + read_start, first_part * sizeof(short));
            memcpy(dec_data->d2 + first_part, circ_buffer, (SAMPLES_PER_CYCLE - first_part) * sizeof(short));
        }
        buffer_mutex.unlock();

        sharedMemory->lock();
        dec_data->params.nutc = nutc;
        dec_data->params.kin = SAMPLES_PER_CYCLE;
        dec_data->params.newdat = true;
        dec_data->ipc[0] = mode.ihsym;
        dec_data->ipc[1] = 1;   // start decode
        dec_data->ipc[2] = -1;  // not done
        sharedMemory->unlock();
        // jt9 signals completion via readyReadStandardOutput → decodeDone().
    }

    // Called at each cycle boundary
    void onCycleTimer() {
        // Triggered mode reuses this timer as a fast poll; dispatch and return.
        if (triggered_) { onTriggeredTick(); return; }

        // Check if jt9 is still running
        if (jt9->state() != QProcess::Running) {
            qStdErr << "Error: jt9 process is not running!\n";
            qStdErr.flush();
            QCoreApplication::quit();
            return;
        }

        // Skip this cycle if previous decode still running (matching WSJT-X behavior)
        if (decode_in_progress) {
            skipped_cycles++;
            qStdErr << "Warning: Previous decode still running, skipping this cycle "
                    << "(total skipped: " << skipped_cycles << ")\n";
            qStdErr.flush();
            return;
        }
        
        // Check if we have enough samples
        if (reader_thread->getTotalSamples() < SAMPLES_PER_CYCLE) {
            qStdErr << "Warning: Not enough samples yet (" << reader_thread->getTotalSamples() << " < " << SAMPLES_PER_CYCLE << ")\n";
            qStdErr.flush();
            return;
        }
        
        // Mark decode as in progress and arm watchdog (2 full cycles as timeout)
        decode_in_progress = true;
        decode_start_ms = getUtcMs();
        decode_watchdog->start(mode.cycle_ms * 2);

        // Get current UTC time
        time_t now = time(NULL);
        struct tm *tm_info = gmtime(&now);
        int nutc = tm_info->tm_hour * 100 + tm_info->tm_min;

        // Calculate precise time for logging
        qint64 utc_ms = getUtcMs();
        qint64 ms_in_minute = utc_ms % 60000;
        double seconds_in_minute = ms_in_minute / 1000.0;

        total_decodes++;
        qStdErr << "Triggering decode #" << total_decodes
                << " at " << QString("%1").arg(nutc, 4, 10, QChar('0'))
                << " +" << QString::number(seconds_in_minute, 'f', 3) << "s"
                << " (" << SAMPLES_PER_CYCLE << " samples)\n";
        qStdErr.flush();

        // Copy audio samples directly to shared memory (matching original working version)
        // No shared memory lock needed for audio data - only buffer_mutex
        buffer_mutex.lock();
        int write_pos = reader_thread->getWritePos();
        int read_start = (write_pos - SAMPLES_PER_CYCLE + BUFFER_SIZE) % BUFFER_SIZE;

        if (read_start + SAMPLES_PER_CYCLE <= BUFFER_SIZE) {
            // Contiguous block - copy directly to shared memory
            memcpy(dec_data->d2, circ_buffer + read_start, SAMPLES_PER_CYCLE * sizeof(short));
        } else {
            // Wraps around - copy in two parts directly to shared memory
            int first_part = BUFFER_SIZE - read_start;
            memcpy(dec_data->d2, circ_buffer + read_start, first_part * sizeof(short));
            memcpy(dec_data->d2 + first_part, circ_buffer, (SAMPLES_PER_CYCLE - first_part) * sizeof(short));
        }
        buffer_mutex.unlock();

        // Lock shared memory to set params and trigger decode atomically
        // This matches the original working version's approach
        sharedMemory->lock();
        dec_data->params.nutc = nutc;
        dec_data->params.kin = SAMPLES_PER_CYCLE;
        dec_data->params.newdat = true;
        dec_data->ipc[0] = mode.ihsym;
        dec_data->ipc[1] = 1;   // start decode
        dec_data->ipc[2] = -1;  // not done
        
        sharedMemory->unlock();
        
        // RETURN IMMEDIATELY - don't wait! (matching WSJT-X line 5651)
        // jt9 will signal us via readyReadStandardOutput when done
    }
    
    // Called if jt9 never sends <DecodeFinished> within 2 cycle periods
    void onDecodeWatchdog() {
        watchdog_fires++;
        qStdErr << "Warning: Decode watchdog fired (total: " << watchdog_fires
                << ") - jt9 did not finish in time, resetting state\n";
        qStdErr.flush();

        // Emit a terminal stats line for THIS cycle so every trigger yields
        // exactly one <DecodeStats cycle_num=N …> — the caller relies on that
        // 1:1 marker to map decode lines to the slot it fed, even on a hang.
        double dur_s = (getUtcMs() - decode_start_ms) / 1000.0;
        qStdOut << "<DecodeStats>"
                << " cycle_num=" << total_decodes
                << " duration_s=" << QString::number(dur_s, 'f', 3)
                << " num_decodes=" << jt9_decode_count
                << " skipped_cycles=" << skipped_cycles
                << " watchdog=1 </DecodeStats>\n";
        qStdOut.flush();

        // Reset flag so the next cycle can trigger a fresh decode
        decode_in_progress = false;
        jt9_decode_count = 0;

        // Force-acknowledge so jt9 is not left waiting for ipc[2]
        sharedMemory->lock();
        dec_data->ipc[2] = 1;
        sharedMemory->unlock();
    }

    // Called when decode is complete (matching WSJT-X decodeDone at line 5717)
    void decodeDone() {
        // Cancel watchdog - clean finish, no recovery needed
        decode_watchdog->stop();
        // Calculate decode duration
        qint64 decode_end_ms = getUtcMs();
        double decode_duration_s = (decode_end_ms - decode_start_ms) / 1000.0;

        // Output machine-readable statistics to stdout
        qStdOut << "<DecodeStats>"
                << " cycle_num=" << total_decodes
                << " duration_s=" << QString::number(decode_duration_s, 'f', 3)
                << " num_decodes=" << jt9_decode_count
                << " skipped_cycles=" << skipped_cycles
                << " watchdog=0 </DecodeStats>\n";
        qStdOut.flush();

        // Clear decode in progress flag BEFORE acknowledgment (matching WSJT-X line 5750)
        decode_in_progress = false;
        jt9_decode_count = 0;

        // Acknowledge decode (matching WSJT-X: to_jt9(m_ihsym, -1, 1) at line 5756)
        sharedMemory->lock();
        dec_data->ipc[2] = 1;  // Tell jt9 we know it has finished
        sharedMemory->unlock();
    }
    
    void jt9Finished(int exitCode, QProcess::ExitStatus exitStatus) {
        qStdErr << "Error: jt9 process exited unexpectedly (code: " << exitCode << ")\n";
        qStdErr.flush();
        QCoreApplication::quit();
    }
    
    void jt9Error(QProcess::ProcessError error) {
        qStdErr << "Error: jt9 process error: " << error << "\n";
        qStdErr.flush();
        QCoreApplication::quit();
    }
    
private:
    qint64 getUtcMs() {
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        return ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL;
    }
    
    qint64 msToNextCycle() {
        qint64 now_ms = getUtcMs();
        qint64 ms_in_cycle = now_ms % mode.cycle_ms;
        return mode.cycle_ms - ms_in_cycle;
    }
    
    static const int TRIGGER_POLL_MS = 50;  // triggered-mode slot poll interval

    QSharedMemory *sharedMemory;
    dec_data_t *dec_data;
    QProcess *jt9;
    ModeConfig mode;
    bool triggered_;        // true = caller-driven per-slot decode (no wall clock)
    qint64 consumed_;       // absolute index of next slot to decode (triggered)

    short *circ_buffer;
    int BUFFER_SIZE;
    int SAMPLES_PER_CYCLE;
    QMutex buffer_mutex;
    AudioReaderThread *reader_thread;
    
    QTimer *cycle_timer;
    QTimer *decode_watchdog;
    bool decode_in_progress;
    int total_decodes;
    int skipped_cycles;
    int jt9_decode_count;
    int watchdog_fires;
    qint64 decode_start_ms;
};

int main(int argc, char *argv[]) {
    QCoreApplication app(argc, argv);
    
    // Create unique application name for this instance (for shared memory key)
    QString unique_app_name = QString("JT9DECODE_%1_%2")
        .arg(QCoreApplication::applicationPid())
        .arg(QDateTime::currentMSecsSinceEpoch());
    app.setApplicationName(unique_app_name);
    
    // Parse command-line arguments
    QString wav_file;
    int depth = 3;               // Decoding depth 1-3
    int freq_low = 100;          // Low frequency Hz (matching WSJT-X typical range)
    int freq_high = 3000;        // High frequency Hz (matching WSJT-X typical range)
    QString jt9_path;
    bool stream_mode = false;    // Stream PCM from stdin
    bool triggered = false;      // Caller-driven per-slot decode (no wall clock)
    bool multithread = false;    // Multithreaded FT8 decoding
    QString mode_str = "FT2";    // Default mode
    const ModeConfig *mode = &MODE_FT2;  // Default to FT2
    
    // Simple argument parser
    int i = 1;
    while (i < argc) {
        QString arg = argv[i];
        
        if (arg == "-d" && i + 1 < argc) {
            depth = QString(argv[++i]).toInt();
        } else if (arg == "-j" && i + 1 < argc) {
            jt9_path = QString(argv[++i]);
        } else if (arg == "-m" && i + 1 < argc) {
            mode_str = QString(argv[++i]).toUpper();
            if (mode_str == "FT2") {
                mode = &MODE_FT2;
            } else if (mode_str == "FT4") {
                mode = &MODE_FT4;
            } else if (mode_str == "FT8") {
                mode = &MODE_FT8;
            } else {
                qStdErr << "Error: Unknown mode '" << mode_str << "'. Valid modes: FT2, FT4, FT8\n";
                qStdErr.flush();
                return 1;
            }
        } else if (arg == "-s") {
            stream_mode = true;
        } else if (arg == "-T" || arg == "--triggered") {
            // Triggered mode also reads PCM from stdin, but the caller owns
            // slot timing: it writes one aligned slot per cadence and we decode
            // each in order, immediately, with no wall-clock alignment.
            triggered = true;
            stream_mode = true;
        } else if (arg == "-t" || arg == "--multithread") {
            multithread = true;
        } else if (arg == "--help" || arg == "-help") {
            qStdErr << "Usage: " << argv[0] << " -j <jt9_path> [options] [<wav_file>|-s]\n";
            qStdErr << "\n";
            qStdErr << "Decode FT2/FT4/FT8 signals from WAV file or stdin stream using jt9\n";
            qStdErr << "\n";
            qStdErr << "Required:\n";
            qStdErr << "  -j <path>     Path to jt9 binary\n";
            qStdErr << "\n";
            qStdErr << "Options:\n";
            qStdErr << "  -m <mode>     Mode: FT2, FT4, or FT8 (default: FT2)\n";
            qStdErr << "                  FT2: 3.75s cycle, 105 symbols\n";
            qStdErr << "                  FT4: 7.5s cycle, 105 symbols\n";
            qStdErr << "                  FT8: 15s cycle, 50 symbols\n";
            qStdErr << "  -d <depth>    Decoding depth 1-3 (default: 3)\n";
            qStdErr << "  -s            Stream mode: read 12kHz 16-bit mono PCM from stdin\n";
            qStdErr << "                Triggers decodes at cycle boundaries aligned to UTC\n";
            qStdErr << "  -T, --triggered  Triggered mode: read 12kHz 16-bit mono PCM from stdin\n";
            qStdErr << "                and decode each fed slot in order, immediately, with NO\n";
            qStdErr << "                wall-clock timing.  The caller writes exactly one slot's\n";
            qStdErr << "                worth of samples per cadence and owns the slot UTC.\n";
            qStdErr << "  -t, --multithread  Enable multithreaded FT8 decoding (FT8 only)\n";
            qStdErr << "                     Uses multiple CPU cores for faster decoding\n";
            qStdErr << "  --help        Show this help message\n";
            qStdErr << "\n";
            qStdErr << "Examples:\n";
            qStdErr << "  # Decode WAV files\n";
            qStdErr << "  " << argv[0] << " -j /usr/local/bin/jt9 recording.wav\n";
            qStdErr << "  " << argv[0] << " -j /opt/jt9 -m FT8 -d 2 recording.wav\n";
            qStdErr << "\n";
            qStdErr << "  # Stream mode (continuous decoding)\n";
            qStdErr << "  rtl_fm -f 144.174M -s 12k | " << argv[0] << " -j /usr/local/bin/jt9 -m FT2 -s\n";
            qStdErr << "  rtl_fm -f 14.074M -s 12k | " << argv[0] << " -j /usr/local/bin/jt9 -m FT8 -s\n";
            qStdErr << "  sox input.wav -t raw -r 12000 -e signed -b 16 -c 1 - | " << argv[0] << " -j jt9 -m FT4 -s\n";
            qStdErr.flush();
            return 0;
        } else if (!arg.startsWith("-")) {
            wav_file = arg;
        } else {
            qStdErr << "Unknown option: " << arg << "\n";
            qStdErr << "Use --help for usage information\n";
            qStdErr.flush();
            return 1;
        }
        i++;
    }
    
    if (!stream_mode && wav_file.isEmpty()) {
        qStdErr << "Error: No WAV file specified (use -s for stream mode)\n";
        qStdErr << "Usage: " << argv[0] << " -j <jt9_path> [options] [<wav_file>|-s]\n";
        qStdErr << "Use --help for more information\n";
        qStdErr.flush();
        return 1;
    }
    
    if (stream_mode && !wav_file.isEmpty()) {
        qStdErr << "Error: Cannot specify both -s (stream mode) and WAV file\n";
        qStdErr.flush();
        return 1;
    }
    
    if (jt9_path.isEmpty()) {
        qStdErr << "Error: jt9 path not specified\n";
        qStdErr << "Usage: " << argv[0] << " -j <jt9_path> [options] [<wav_file>|-s]\n";
        qStdErr << "Use --help for more information\n";
        qStdErr.flush();
        return 1;
    }
    
    // Create shared memory with Qt
    QSharedMemory sharedMemory;
    sharedMemory.setKey(app.applicationName());
    
    // Try to attach first (in case it exists from previous run)
    if (sharedMemory.attach()) {
        qStdErr << "Detaching from existing shared memory\n";
        sharedMemory.detach();
    }
    
    // Create new shared memory
    if (!sharedMemory.create(sizeof(dec_data_t))) {
        qStdErr << "Failed to create shared memory: " << sharedMemory.errorString() << "\n";
        qStdErr.flush();
        return 1;
    }
    
    qStdErr << "Shared memory created with key: " << app.applicationName() << "\n";
    qStdErr << "Structure size: " << sizeof(dec_data_t) << " bytes\n";
    qStdErr << "\n";
    
    // Lock and initialize
    sharedMemory.lock();
    dec_data_t *dec_data = static_cast<dec_data_t*>(sharedMemory.data());
    memset(dec_data, 0, sizeof(dec_data_t));
    
    // Set up common parameters for decoding (matching WSJT-X lines 5430-5490)
    dec_data->params.nmode = mode->mode_code;  // Mode code (52=FT2, 5=FT4, 8=FT8)
    dec_data->params.ntrperiod = mode->cycle_ms / 1000;  // TR period in seconds
    dec_data->params.ndepth = depth;
    dec_data->params.nfa = freq_low;
    dec_data->params.nfb = freq_high;
    dec_data->params.nfqso = 1500;
    dec_data->params.nftx = 1500;  // TX frequency (not used in RX-only mode but must be set)
    dec_data->params.ntol = 100;
    dec_data->params.nagain = false;
    dec_data->params.nQSOProgress = 0;
    dec_data->params.lapcqonly = false;  // CRITICAL: false for normal RX (true would only decode CQ messages)
    dec_data->params.nsubmode = 0;
    dec_data->params.ndiskdat = true;  // TESTING: Try true for both modes
    dec_data->params.lmultift8 = multithread;  // Enable multithreaded FT8 (FT8 only)
    dec_data->params.nzhsym = mode->nzhsym;  // hsymStop - critical for decode count (WSJT-X line 2466)
    
    // yymmdd for non-disk data (WSJT-X line 5396)
    if (stream_mode) {  // FIXED: was !stream_mode
        dec_data->params.yymmdd = -1;  // Critical for streaming mode
    }
    
    // Critical parameters for decode count (WSJT-X lines 5431-5434)
    dec_data->params.n2pass = 1;  // FIXED: Was 2, should be 1 (WSJT-X line 5431)
    dec_data->params.nranera = 10000;  // Random erasure trials (WSJT-X default)
    dec_data->params.naggressive = 0;  // Aggressive level (0=normal, WSJT-X line 5434)
    dec_data->params.nrobust = 0;  // Robust mode off (WSJT-X line 5435)
    
    // FT8 AP (a priori) decoding - CRITICAL for decode count (WSJT-X lines 5476-5478)
    if (mode->mode_code == 8) {  // FT8
        dec_data->params.lft8apon = true;  // Enable AP decoding
        dec_data->params.napwid = 50;  // AP bandwidth (default for HF)
    } else {
        dec_data->params.lft8apon = false;
        dec_data->params.napwid = 0;
    }

    // Set datetime (YYYYMMDD_HHMMSS format)
    QDateTime now = QDateTime::currentDateTimeUtc();
    QString datetime_str = now.toString("yyyyMMdd_HHmmss");
    strncpy(dec_data->params.datetime, datetime_str.toLatin1().constData(), 20);
    
    strncpy(dec_data->params.mycall, "K1ABC", 12);
    strncpy(dec_data->params.mygrid, "FN20", 6);
    strncpy(dec_data->params.hiscall, "", 12);  // Empty for RX-only
    strncpy(dec_data->params.hisgrid, "", 6);   // Empty for RX-only
    
    sharedMemory.unlock();
    
    // Create unique temporary directory in /dev/shm for this instance
    QString temp_dir_path = QString("/dev/shm/jt9_decode_%1_%2")
        .arg(QCoreApplication::applicationPid())
        .arg(QDateTime::currentMSecsSinceEpoch());
    
    QDir temp_dir;
    if (!temp_dir.mkpath(temp_dir_path)) {
        qStdErr << "Warning: Could not create temp directory in /dev/shm, falling back to /tmp\n";
        temp_dir_path = "/tmp";
    } else {
        qStdErr << "Created temp directory: " << temp_dir_path << "\n";
    }
    
    // Start jt9 process
    qStdErr << "Starting jt9 decoder...\n";
    qStdErr.flush();
    
    QProcess jt9;
    QStringList args;
    args << "-s" << app.applicationName()
         << "-w" << "1"
         << "-m" << "1"
         << "-e" << "."
         << "-a" << "."
         << "-t" << temp_dir_path;
    
    // Verify jt9 binary exists
    if (!QFile::exists(jt9_path)) {
        qStdErr << "jt9 binary not found at: " << jt9_path << "\n";
        qStdErr.flush();
        return 1;
    }
    
    qStdErr << "Using jt9 at: " << jt9_path << "\n";
    qStdErr.flush();
    
    // Capture jt9 output
    jt9.setProcessChannelMode(QProcess::MergedChannels);
    jt9.start(jt9_path, args);
    
    if (!jt9.waitForStarted()) {
        qStdErr << "Failed to start jt9: " << jt9.errorString() << "\n";
        qStdErr.flush();
        return 1;
    }
    
    qStdErr << "\nDecoder parameters:\n";
    qStdErr << "  Mode: " << mode->name << " (" << mode->mode_code << ")\n";
    qStdErr << "  Cycle time: " << (mode->cycle_ms / 1000.0) << " seconds\n";
    qStdErr << "  Depth: " << depth << "\n";
    qStdErr << "  Frequency range: " << freq_low << " - " << freq_high << " Hz\n";
    if (multithread && mode->mode_code == 8) {
        qStdErr << "  Multithreaded: enabled (FT8)\n";
    }
    qStdErr.flush();
    
    int result = 0;
    
    if (stream_mode) {
        // Streaming mode: asynchronous event-driven processing (WSJT-X style).
        // `triggered` selects caller-driven per-slot decode vs wall-clock cycles.
        StreamDecoder decoder(&sharedMemory, dec_data, &jt9, *mode, triggered);
        decoder.start();
        
        // Run Qt event loop - processes jt9 output asynchronously
        result = app.exec();
        
        // Cleanup: terminate jt9
        qStdErr << "Terminating jt9...\n";
        sharedMemory.lock();
        dec_data->ipc[1] = 999;
        sharedMemory.unlock();
        
        if (!jt9.waitForFinished(2000)) {
            jt9.kill();
            jt9.waitForFinished();
        }
    } else {
        // WAV file mode: read file and decode once
        sharedMemory.lock();
        dec_data = static_cast<dec_data_t*>(sharedMemory.data());
        
        qStdErr << "\nReading WAV file: " << wav_file << "\n";
        int nsamples = read_wav_file(wav_file, dec_data->d2, NTMAX*RX_SAMPLE_RATE);
        if (nsamples < 0) {
            sharedMemory.unlock();
            jt9.kill();
            jt9.waitForFinished();
            return 1;
        }
        
        // Set up parameters for this decode
        time_t now = time(NULL);
        struct tm *tm_info = gmtime(&now);
        
        dec_data->params.nutc = tm_info->tm_hour * 100 + tm_info->tm_min;
        dec_data->params.kin = nsamples;
        dec_data->params.newdat = true;
        
        qStdErr << "  Samples: " << nsamples << "\n";
        qStdErr << "  UTC: " << QString("%1").arg(dec_data->params.nutc, 4, 10, QChar('0')) << "\n";
        qStdErr << "\n";
    
        // Signal jt9 to start decoding
        dec_data->ipc[0] = mode->ihsym;  // ihsym (105 for FT2/FT4, 50 for FT8)
        dec_data->ipc[1] = 1;             // start decoding
        dec_data->ipc[2] = -1;            // not done
        
        sharedMemory.unlock();
        
        // Give jt9 time to decode
        QThread::sleep(2);
        
        // Acknowledge decode is done
        sharedMemory.lock();
        dec_data = static_cast<dec_data_t*>(sharedMemory.data());
        dec_data->ipc[2] = 1;  // acknowledge
        sharedMemory.unlock();
        
        QThread::msleep(100);
        
        // Tell jt9 to terminate
        sharedMemory.lock();
        dec_data->ipc[1] = 999;  // terminate
        sharedMemory.unlock();
        
        // Wait for jt9 to finish and capture output
        if (!jt9.waitForFinished(5000)) {
            qStdErr << "jt9 didn't exit cleanly, killing...\n";
            qStdErr.flush();
            jt9.kill();
            jt9.waitForFinished();
        }
        
        // Process jt9 output
        QString output = QString::fromLocal8Bit(jt9.readAllStandardOutput());
        QStringList lines = output.split('\n', Qt::SkipEmptyParts);
        
        for (const QString &line : lines) {
            if (line.length() > 6 && line[0].isDigit() && !line.startsWith('<')) {
                qStdOut << line << "\n";
                qStdOut.flush();
            } else {
                qStdErr << line << "\n";
            }
        }
        
        qStdErr << "jt9 finished with exit code: " << jt9.exitCode() << "\n";
        qStdErr.flush();
    }
    
    // Cleanup temp directory if we created one in /dev/shm
    if (temp_dir_path.startsWith("/dev/shm/jt9_decode_")) {
        QDir temp_dir(temp_dir_path);
        if (temp_dir.exists()) {
            if (temp_dir.removeRecursively()) {
                qStdErr << "Cleaned up temp directory: " << temp_dir_path << "\n";
            } else {
                qStdErr << "Warning: Could not remove temp directory: " << temp_dir_path << "\n";
            }
        }
    }
    
    return result;
}

// Include moc file for Qt meta-object compilation
#include "jt9_decode.moc"
