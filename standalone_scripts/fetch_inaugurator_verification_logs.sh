#!/bin/bash
nr_files_to_reserve=10
match_pattern="Starting Verification"

# create dirs if not exist:
echo "Creating Directories"
mkdir `find /var/lib/rackattackphysical/seriallogs/ -name *.txt | cut -d "/" -f 6 | cut -d "-" -f 1-2` >& /dev/null

servers=$(grep -l "$match_pattern" /var/lib/rackattackphysical/seriallogs/*)


if [ ! -z "$servers" -a "$servers" != "" ]; then
        echo `date` ":"

        for server in $servers; do
                match_instance=$(grep -a "$match_pattern" $server | cut -d " " -f 1-2 | md5sum | cut -f 1 -d " ")
                servername=$(echo $server | cut -d "/" -f 6 | cut -d "-" -f 1-2)

                cd $servername

                # Skip if this match instance was already found
                if [ -f "$match_instance.log" ]; then
                        echo
                        echo "File $servername/$match_instance.log already exists"
                        cd ..
                        continue
                fi

                echo
                echo "$servername: Removing old log files (reserving only the $nr_files_to_reserve newest matched log files)"
                files_to_reserve=$(echo `ls -1 -t *.log | head -$nr_files_to_reserve | tr "\\n" "|"` | sed "s/|\+$//")
                echo "Files to reserve: $files_to_reserve"
                if [[ "$files_to_reserve" == "*rack*" ]]; then
                        files_to_remove=$(ls | egrep -v "$files_to_reserve")
                        echo "Files to remove: $files_to_remove"
                        rm $files_to_remove
                else
                        echo "Not removing files, since there aren't enough files in dir"
                fi
                cp $server "$match_instance.log"
                cd ..
        done

fi
