// Emulates MOLD_JOBS=N for any N; mold 3.0.0 honours only MOLD_JOBS=1.
// Waits for one of N slots of a named POSIX semaphore, runs mold with the
// given arguments, then releases the slot. Like mold's own MOLD_JOBS=1
// lock, the slot is held until the mold process exits.
//
// usage: MOLD_REAL=/path/to/mold MOLD_JOBS_N=4 MOLD_JOBS_SEM=/name moldjobs ARGS...
// build: cc -O2 -o moldjobs moldjobs.c
#include <fcntl.h>
#include <semaphore.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

int main(int argc, char **argv) {
  const char *real = getenv("MOLD_REAL"), *name = getenv("MOLD_JOBS_SEM"), *n = getenv("MOLD_JOBS_N");
  if (!real || !name || !n || atoi(n) < 1) {
    fputs("moldjobs: set MOLD_REAL, MOLD_JOBS_SEM and MOLD_JOBS_N\n", stderr);
    return 2;
  }
  sem_t *sem = sem_open(name, O_CREAT, 0600, (unsigned)atoi(n));
  if (sem == SEM_FAILED) {
    perror("sem_open");
    return 2;
  }
  while (sem_wait(sem) != 0)
    ;
  int status = 1 << 8;
  pid_t pid = fork();
  if (pid == 0) {
    argv[0] = (char *)real;
    execv(real, argv);
    perror(real);
    _exit(127);
  }
  if (pid > 0)
    waitpid(pid, &status, 0);
  sem_post(sem);
  return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
