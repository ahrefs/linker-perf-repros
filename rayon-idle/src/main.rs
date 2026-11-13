//! A program shaped like a small mold link: a series of short parallel
//! phases on a Rayon pool, separated by short serial steps. It reports wall
//! time and the CPU time of the whole process, so the CPU spent by idle
//! workers shows up as the difference between `--threads 1` and more.
//!
//! usage: rayon-idle [--threads N] [--phases P] [--items I] [--item-us U] [--serial-us S]

use rayon::prelude::*;
use std::hint::black_box;
use std::time::{Duration, Instant};

fn spin(d: Duration) {
    let end = Instant::now() + d;
    let mut x = 0u64;
    while Instant::now() < end {
        for i in 0..64 {
            x = black_box(x.wrapping_mul(6364136223846793005).wrapping_add(i));
        }
    }
    black_box(x);
}

fn cpu_ms() -> (f64, f64) {
    let mut ru: libc::rusage = unsafe { std::mem::zeroed() };
    unsafe { libc::getrusage(libc::RUSAGE_SELF, &mut ru) };
    let ms = |t: libc::timeval| t.tv_sec as f64 * 1e3 + t.tv_usec as f64 / 1e3;
    (ms(ru.ru_utime), ms(ru.ru_stime))
}

fn main() {
    let mut threads = 32usize;
    let (mut phases, mut items, mut item_us, mut serial_us) = (50usize, 64usize, 2.5f64, 30f64);
    let mut args = std::env::args().skip(1);
    while let Some(flag) = args.next() {
        let value = args.next().unwrap_or_else(|| panic!("{flag} needs a value"));
        match flag.as_str() {
            "--threads" => threads = value.parse().unwrap(),
            "--phases" => phases = value.parse().unwrap(),
            "--items" => items = value.parse().unwrap(),
            "--item-us" => item_us = value.parse().unwrap(),
            "--serial-us" => serial_us = value.parse().unwrap(),
            _ => panic!("unknown flag {flag}"),
        }
    }
    rayon::ThreadPoolBuilder::new().num_threads(threads).build_global().unwrap();
    let item = Duration::from_secs_f64(item_us / 1e6);
    let serial = Duration::from_secs_f64(serial_us / 1e6);

    let start = Instant::now();
    for _ in 0..phases {
        spin(serial);
        (0..items).into_par_iter().for_each(|_| spin(item));
    }
    let wall = start.elapsed().as_secs_f64() * 1e3;
    let (user, sys) = cpu_ms();
    let work = phases as f64 * (serial_us + items as f64 * item_us) / 1e3;
    println!("threads {threads} work {work:.1} ms wall {wall:.1} ms user {user:.1} ms sys {sys:.1} ms");
}
