# The SSH/SFTP lab of scripts/ssh-lab.sh: OpenSSH with the internal sftp-server
# subsystem, a "lab" user that accepts a password and public keys, and a small
# tree with a folder, a symbolic link and a 64 MiB file for the Files place's
# live tests. Throwaway credentials, bound to 127.0.0.1 only. PerSourcePenalties
# is off so the deliberate wrong-password checks do not lock out the next test.
FROM alpine:3
RUN apk add --no-cache openssh-server openssh-sftp-server \
 && ssh-keygen -A \
 && adduser -D -s /bin/sh lab \
 && echo 'lab:labpass' | chpasswd \
 && sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config \
 && sed -i 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
 && echo 'PerSourcePenalties no' >> /etc/ssh/sshd_config \
 && mkdir -p /home/lab/.ssh /home/lab/data/logs/archive \
 && dd if=/dev/urandom of=/home/lab/data/big.bin bs=1M count=64 2>/dev/null \
 && printf 'hello\n' > /home/lab/data/hello.txt \
 && printf 'error line\n' > /home/lab/data/logs/mysqld.err \
 && printf 'old\n' > /home/lab/data/logs/archive/mysqld.err.1 \
 && printf 'hidden\n' > /home/lab/data/.hidden \
 && ln -s /home/lab/data/logs /home/lab/logs-link \
 && ln -s /etc/hostname /home/lab/data/host-link \
 && chown -R lab:lab /home/lab && chmod 700 /home/lab/.ssh
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
