//! nakoscope-capture — high-speed VDS1022 USB capture binary.
//!
//! Streams raw ADC frames to stdout in a simple binary format so that the
//! Python nakoscope stack (recorder, storage, MCP) can operate unchanged.
//!
//! USB protocol derived from the excellent florentbr/OWON-VDS1022 Python API:
//! <https://github.com/florentbr/OWON-VDS1022>
//!
//! # Output format
//!
//! One JSON line on stderr when connected:
//!   {"event":"connected","device":"VDS1022","sample_rate":50000,
//!    "v_range_per_div":1.0,"sy":0.004,"ty":0.0,"channels":[0]}
//!
//! Then a stream of binary frame packets on stdout:
//!   [magic: 4 bytes b"NSC\0"] [channel: u8] [n_samples: u32 LE] [samples: i8 * n_samples]
//!
//! Python converts: voltage = sample_i8 * sy + ty

use std::io::{self, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use clap::Parser;
use rusb::{DeviceHandle, Direction, GlobalContext, TransferType};

// ── VDS1022 USB constants ──────────────────────────────────────────────────────

const VID: u16 = 0x5345;
const PID: u16 = 0x1234;
const IFACE: u8 = 0;
const TIMEOUT: Duration = Duration::from_millis(200);

const ADC_SIZE: usize   = 5100;  // 50 pre + 5000 samples + 50 post
const FRAME_SIZE: usize = 5211;  // 11 header + 100 trigger + 5100 ADC
const ADC_OFFSET: usize = 111;   // FRAME_SIZE - ADC_SIZE
const ADC_RANGE: f32    = 250.0;

const VOLT_RANGES: [f32; 10] = [0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0];
const MAX_SAMPLE_RATE: f32   = 100e6;

// ── Command addresses ──────────────────────────────────────────────────────────

const CMD_GET_MACHINE:   u32      = 0x4001;
const CMD_GET_DATA:      u32      = 0x1000;
const CMD_GET_TRIGGERED: u32      = 0x0001;
const CMD_SET_ROLLMODE:  u32      = 0x000a;
const CMD_SET_SUF_TRG:   u32      = 0x0056;
const CMD_SET_PRE_TRG:   u32      = 0x005a;
const CMD_SET_DEEPMEM:   u32      = 0x005c;
const CMD_SET_TIMEBASE:  u32      = 0x0052;
const CMD_SET_ZERO_OFF:  [u32; 2] = [0x010a, 0x0108];
const CMD_SET_VOLT_GAIN: [u32; 2] = [0x0116, 0x0114];
const CMD_SET_CHANNEL:   [u32; 2] = [0x0111, 0x0110];

const STATUS_E: u8 = b'E'; // "not ready" response

// ── CLI ───────────────────────────────────────────────────────────────────────

#[derive(Parser, Debug)]
#[command(name = "nakoscope-capture", about = "High-speed VDS1022 USB capture")]
struct Args {
    /// Sampling rate in samples/sec (e.g. 1250000)
    #[arg(long, default_value_t = 50_000.0f32)]
    rate: f32,

    /// Channels to capture: 1, 2, or both
    #[arg(long, default_value = "1")]
    channels: String,

    /// Full-scale voltage range in volts (e.g. 10 = ±5V, 1 = ±0.5V)
    #[arg(long, default_value_t = 10.0f32)]
    v_range: f32,

    /// Coupling: dc or ac
    #[arg(long, default_value = "dc")]
    coupling: String,

    /// Probe attenuation: 1 or 10
    #[arg(long, default_value_t = 1u32)]
    probe: u32,
}

// ── Command packing ───────────────────────────────────────────────────────────

/// IBB: address:u32 + size=1:u8 + value:u8 → 6 bytes
fn pack_ibb(address: u32, value: u8) -> [u8; 6] {
    let mut b = [0u8; 6];
    b[0..4].copy_from_slice(&address.to_le_bytes());
    b[4] = 1;
    b[5] = value;
    b
}

/// IBH: address:u32 + size=2:u8 + value:u16 → 7 bytes
fn pack_ibh(address: u32, value: u16) -> [u8; 7] {
    let mut b = [0u8; 7];
    b[0..4].copy_from_slice(&address.to_le_bytes());
    b[4] = 2;
    b[5..7].copy_from_slice(&value.to_le_bytes());
    b
}

/// IBI: address:u32 + size=4:u8 + value:u32 → 9 bytes
fn pack_ibi(address: u32, value: u32) -> [u8; 9] {
    let mut b = [0u8; 9];
    b[0..4].copy_from_slice(&address.to_le_bytes());
    b[4] = 4;
    b[5..9].copy_from_slice(&value.to_le_bytes());
    b
}

// ── Device ────────────────────────────────────────────────────────────────────

struct Device {
    handle:   DeviceHandle<GlobalContext>,
    ep_write: u8,
    ep_read:  u8,
    rx_buf:   Vec<u8>,
}

impl Device {
    fn open() -> rusb::Result<Self> {
        let handle = rusb::open_device_with_vid_pid(VID, PID)
            .ok_or(rusb::Error::NotFound)?;
        handle.claim_interface(IFACE)?;

        let (ep_write, ep_read) = {
            let dev = handle.device();
            let config = dev.active_config_descriptor()?;
            let mut w = None;
            let mut r = None;
            'outer: for iface in config.interfaces() {
                for desc in iface.descriptors() {
                    for ep in desc.endpoint_descriptors() {
                        if ep.transfer_type() == TransferType::Bulk {
                            match ep.direction() {
                                Direction::Out => w = Some(ep.address()),
                                Direction::In  => r = Some(ep.address()),
                            }
                        }
                    }
                    if w.is_some() && r.is_some() { break 'outer; }
                }
            }
            (w.ok_or(rusb::Error::NotFound)?, r.ok_or(rusb::Error::NotFound)?)
        };

        Ok(Device { handle, ep_write, ep_read, rx_buf: vec![0u8; FRAME_SIZE] })
    }

    fn write(&mut self, buf: &[u8]) -> rusb::Result<()> {
        self.handle.write_bulk(self.ep_write, buf, TIMEOUT)?;
        Ok(())
    }

    fn read5(&mut self) -> rusb::Result<u32> {
        self.handle.read_bulk(self.ep_read, &mut self.rx_buf[..5], TIMEOUT)?;
        Ok(u32::from_le_bytes(self.rx_buf[1..5].try_into().unwrap()))
    }

    fn send_ibb(&mut self, addr: u32, val: u8)  -> rusb::Result<u32> { self.write(&pack_ibb(addr, val))?; self.read5() }
    fn send_ibh(&mut self, addr: u32, val: u16) -> rusb::Result<u32> { self.write(&pack_ibh(addr, val))?; self.read5() }
    fn send_ibi(&mut self, addr: u32, val: u32) -> rusb::Result<u32> { self.write(&pack_ibi(addr, val))?; self.read5() }
}

// ── Frame output ──────────────────────────────────────────────────────────────

/// Write one binary frame to stdout:
/// b"NSC\0" + channel:u8 + n_samples:u32_le + samples:i8*n
fn write_frame(out: &mut io::StdoutLock, channel: u8, samples: &[i8]) -> io::Result<()> {
    let n = samples.len() as u32;
    out.write_all(b"NSC\0")?;
    out.write_all(&[channel])?;
    out.write_all(&n.to_le_bytes())?;
    let bytes = unsafe { std::slice::from_raw_parts(samples.as_ptr() as *const u8, samples.len()) };
    out.write_all(bytes)?;
    out.flush()
}

// ── Main ──────────────────────────────────────────────────────────────────────

fn main() {
    let args = Args::parse();

    let running = Arc::new(AtomicBool::new(true));
    let r = running.clone();
    ctrlc::set_handler(move || r.store(false, Ordering::Relaxed))
        .expect("Failed to set Ctrl+C handler");

    // Open device
    let mut dev = match Device::open() {
        Ok(d) => d,
        Err(e) => {
            eprintln!("{{\"event\":\"error\",\"msg\":\"Cannot open VDS1022: {e}\"}}");
            std::process::exit(1);
        }
    };

    // Verify machine type
    match dev.send_ibb(CMD_GET_MACHINE, b'V') {
        Ok(1) => {}
        Ok(v) => { eprintln!("{{\"event\":\"error\",\"msg\":\"Unexpected machine type: {v}\"}}"); std::process::exit(1); }
        Err(e) => { eprintln!("{{\"event\":\"error\",\"msg\":\"GET_MACHINE failed: {e}\"}}"); std::process::exit(1); }
    }

    // Parse channel selection
    let ch_on = [args.channels != "2", args.channels == "2" || args.channels == "both"];
    let chl_cnt = ch_on.iter().filter(|&&x| x).count();
    let coupling: u8 = if args.coupling.to_lowercase() == "ac" { 1 } else { 0 };

    // Select voltage range
    let v_per_div = args.v_range / 10.0;
    let (vb, vr) = VOLT_RANGES.iter().enumerate()
        .min_by(|(_, a), (_, b)| ((*a - v_per_div).abs()).partial_cmp(&((*b - v_per_div).abs())).unwrap())
        .map(|(i, &v)| (i, v)).unwrap();
    let sy = vr / ADC_RANGE;
    let ty = 0.0f32;
    let attenuate = vb >= 6; // hardware relay for ≥2V/div ranges

    // Select sampling rate
    let prescaler = (MAX_SAMPLE_RATE / args.rate).round().max(1.0) as u32;
    let actual_rate = MAX_SAMPLE_RATE / prescaler as f32;

    // Configure channels
    for chl in 0..2usize {
        if !ch_on[chl] {
            let _ = dev.send_ibb(CMD_SET_CHANNEL[chl], 0); // off
            continue;
        }
        let _ = dev.send_ibh(CMD_SET_ZERO_OFF[chl], 2048);  // mid-scale (no calibration)
        let _ = dev.send_ibh(CMD_SET_VOLT_GAIN[chl], 2048); // mid-scale
        let chl_arg: u8 = ((attenuate as u8) << 1) | (coupling << 5) | (1 << 7);
        let _ = dev.send_ibb(CMD_SET_CHANNEL[chl], chl_arg);
    }

    // Configure sampling rate + roll mode
    let _ = dev.send_ibi(CMD_SET_TIMEBASE, prescaler);
    let _ = dev.send_ibb(CMD_SET_ROLLMODE, 1);

    // Set up roll mode capture parameters
    let adc_circ = ADC_SIZE + 20;
    let _ = dev.send_ibi(CMD_SET_SUF_TRG, 0);
    let _ = dev.send_ibh(CMD_SET_PRE_TRG, adc_circ as u16);
    let _ = dev.send_ibh(CMD_SET_DEEPMEM, adc_circ as u16);
    let _ = dev.send_ibb(CMD_SET_ROLLMODE, 1);

    // Wait up to 600ms for trigger to clear
    for _ in 0..6 {
        if !running.load(Ordering::Relaxed) { return; }
        std::thread::sleep(Duration::from_millis(100));
        if let Ok(0) = dev.send_ibb(CMD_GET_TRIGGERED, 0) { break; }
    }

    // Build GET_DATA command (CH1 on=5/off=4, CH2 same, packed as little-endian u16)
    let cmd_get = pack_ibh(CMD_GET_DATA,
        (if ch_on[0] { 5u16 } else { 4 }) | ((if ch_on[1] { 5u16 } else { 4 }) << 8));

    // Emit connected event
    let ch_list: Vec<String> = (0..2).filter(|&i| ch_on[i]).map(|i| i.to_string()).collect();
    eprintln!(
        "{{\"event\":\"connected\",\"device\":\"VDS1022\",\"sample_rate\":{actual_rate},\
         \"v_range_per_div\":{vr},\"sy\":{sy},\"ty\":{ty},\"channels\":[{}]}}",
        ch_list.join(","),
    );

    // Timing parameters
    let delay = Duration::from_secs_f32(
        ((ADC_SIZE as f32 / actual_rate / 4.0) - (0.035 * chl_cnt as f32)).max(0.0)
    );
    let maxtime = Duration::from_secs_f32(ADC_SIZE as f32 / actual_rate);

    let stdout = io::stdout();
    let mut out = stdout.lock();
    let mut cursors = [20usize; 2];
    let mut last_read = Instant::now() + Duration::from_secs(5);

    // ── Capture loop ──────────────────────────────────────────────────────────
    while running.load(Ordering::Relaxed) {
        if !delay.is_zero() {
            std::thread::sleep(delay);
        }

        if dev.write(&cmd_get).is_err() { break; }

        for _ in 0..chl_cnt {
            let n = match dev.handle.read_bulk(dev.ep_read, &mut dev.rx_buf, TIMEOUT) {
                Ok(n) => n,
                Err(_) => break,
            };

            // Not-ready response
            if n == 5 && dev.rx_buf[0] == STATUS_E { continue; }
            if n != FRAME_SIZE { continue; }

            let chl = dev.rx_buf[0] as usize;
            if chl >= 2 { continue; }

            let cursor = u16::from_le_bytes([dev.rx_buf[9], dev.rx_buf[10]]) as usize;
            let new_n = (cursor + adc_circ - cursors[chl]) % adc_circ;
            if new_n == 0 { continue; }
            cursors[chl] = cursor;

            // Timing slip warning (don't abort — just log)
            let now = Instant::now();
            if now.duration_since(last_read) > maxtime {
                eprintln!("{{\"event\":\"warning\",\"msg\":\"timing slip — samples may be missing\"}}");
            }
            last_read = now;

            // Extract new samples from the tail of the ADC buffer
            let adc = &dev.rx_buf[ADC_OFFSET..FRAME_SIZE];
            let raw = &adc[ADC_SIZE - new_n..ADC_SIZE];
            let samples: &[i8] = unsafe {
                std::slice::from_raw_parts(raw.as_ptr() as *const i8, new_n)
            };

            if write_frame(&mut out, chl as u8, samples).is_err() {
                running.store(false, Ordering::Relaxed);
                break;
            }
        }
    }

    eprintln!("{{\"event\":\"stopped\"}}");
}
