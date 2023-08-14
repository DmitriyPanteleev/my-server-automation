# Распаковка архивов
# example: extract file
extract () {
    if [ -f $1 ] ; then
        case $1 in
            *.deb)      ar vx $1        ;;
            *.tar.bz2)  tar xjf $1      ;;
            *.tar.gz)   tar xzf $1      ;;
            *.tar.xz)   tar xJf $1      ;;
            *.bz2)      bunzip2 $1      ;;
            *.rar)      unrar x $1      ;;
            *.gz)       gunzip $1       ;;
            *.tar)      tar xf $1       ;;
            *.tbz2)     tar xjf $1      ;;
            *.tbz)      tar -xjvf $1    ;;
            *.tgz)      tar xzf $1      ;;
            *.docx)     unzip $1        ;;
            *.zip)      unzip $1        ;;
            *.Z)        uncompress $1   ;;
            *.7z)       7z x $1         ;;
            *)          echo "'$1' неизвестный тип архива" ;;
        esac
    else
        echo "'$1' файл не найден"
    fi
}

# Запаковать архив
# example: archive tar file
archive () {
    if [ $1 ] ; then
        case $1 in
            tbz)        tar cjvf $2.tar.bz2 $2      ;;
            tgz)        tar czvf $2.tar.gz  $2      ;;
            tar)        tar cpvf $2.tar  $2         ;;
            bz2)        bzip $2                     ;;
            gz)         gzip -c -9 -n $2 > $2.gz    ;;
            zip)        zip -r $2.zip $2            ;;
            7z)         7z a $2.7z $2               ;;
            *)          echo "'$1' неизвестный тип архива" ;;
        esac
    else
        echo "'$2' файл не найден"
    fi
}
